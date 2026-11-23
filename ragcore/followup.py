"""Resolve short follow-ups from the signed-in user's selected history branch."""
import json
import re

from .business_answers import matched_metrics, operation_hint
from . import retrieval


def context(store, owner, parent_id, question, mode, selected_id):
    if parent_id is None:
        return question, selected_id, {}
    with store.db() as conn:
        row = conn.execute("SELECT result FROM history WHERE id=? AND owner=?", (parent_id, owner)).fetchone()
    if not row:
        raise KeyError("历史会话不存在")
    parent = json.loads(row["result"])
    compact = question.strip(" ？?。！!")
    relative = bool(re.search(r"^(?:那么?|换成|改为|还是|再看|继续|上面|上述|刚才|其中|what about\b)|呢$",
                              compact, re.I))
    if not relative:
        return question, selected_id, {}
    if parent["intent"] == "documents" and mode in {"documents","auto"} and re.search(
            r"上面|上述|刚才|其中|这条|这个|第.{1,3}项", question):
        return parent["question"][:500] + "。" + question, selected_id, {}
    if mode == "documents" or parent.get("connector_id") is None or selected_id not in (None, parent["connector_id"]):
        return question, selected_id, {}
    configs = store.connectors()
    config = next((item for item in configs if item["id"] == parent["connector_id"]), None)
    if not config:
        return question, selected_id, {}
    explicit = retrieval.route_question(question, configs, "auto")
    if (explicit["connector_id"] not in (None, config["id"]) or
            (explicit["intent"] == "documents" and retrieval._DOC.search(retrieval._normal(question)))):
        return question, selected_id, {}
    new_values, _, ambiguous = retrieval._parameters(question, config)
    current_metrics = matched_metrics(config, question)
    metric_labels = [m["label"] for m in current_metrics] or parent.get("metric_labels", [])
    if not metric_labels:
        metric_labels = [m["label"] for m in matched_metrics(config, parent["question"])]
    if not (new_values or ambiguous or current_metrics or re.search(r"全部|所有|不限|继续|同样|同一", compact)):
        return question, selected_id, {}
    carried = {}
    for param in config["parameters"]:
        key = param["name"]
        name = "|".join(re.escape(v) for v in [param["label"], key, *param.get("aliases", [])])
        reset = re.search(rf"(?:所有|全部|各个|不限)(?:的)?(?:{name})|(?:{name})不限", question, re.I)
        if key in parent.get("params", {}) and key not in ambiguous and not reset:
            carried[key] = parent["params"][key]
    hints = [] if current_metrics else list(metric_labels)
    if not current_metrics and not operation_hint(question):
        previous_operation = parent.get("operation_hint") or operation_hint(parent["question"])
        if previous_operation:
            hints.append(previous_operation)
    return "，".join([*hints, question]), config["id"], carried
