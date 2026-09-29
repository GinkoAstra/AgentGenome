"""B1: strict definitions, method selection, and durable, scoped Skill authoring.

The built-in text recognizer deliberately covers one documented orders Skill.
It is a protocol fixture, not a claim of general natural-language compilation.
Neither compilation nor authoring invokes a business capability or verifier.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from pathlib import Path
import re
import uuid

from .common import SopError, canonical, digest, file_digest, read_json, write_json


RULES = ["highest_revision", "first"]
CAPABILITIES = ["orders.profile", "orders.normalize", "orders.deduplicate", "orders.summarize"]
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_BINDING = re.compile(r"^\$(input\.([A-Za-z][A-Za-z0-9_-]*)|steps\.([A-Za-z][A-Za-z0-9_-]*)\.([A-Za-z][A-Za-z0-9_-]*))$")



def _problem(code, path, message):
    return {"code": code, "path": path, "message": message}


def _is_type(value, kind):
    return {"string": lambda: isinstance(value, str),
            "integer": lambda: type(value) is int,
            "boolean": lambda: type(value) is bool,
            "object": lambda: type(value) is dict,
            "artifact": lambda: False}.get(kind, lambda: False)()


def validate_definition(definition, registry):
    def strict(value):
        if value is None or type(value) in (str, bool, int, float):
            return
        if type(value) is list:
            for item in value:
                strict(item)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                strict(item)
            return
        raise SopError("invalid_json", "only JSON values and string object keys are permitted")

    try:
        strict(definition)
        return _validate_definition(definition, registry)
    except (SopError, TypeError, AttributeError, KeyError, ValueError) as exc:
        return {"passed": False, "diagnostics": [_problem(getattr(exc, "code", "invalid_schema"), "$", str(exc))],
                "definition_hash": None, "dependency_hashes": {}, "checker_versions": {}}


def _validate_definition(definition, registry):
    """Compile a sop/1 definition without executing any declared operation.

    Diagnostics are structured and fail closed. Registry hashes are dependencies,
    not an authorization grant; B2 separately intersects the original run grant.
    """
    diagnostics = []
    result = {"passed": False, "diagnostics": diagnostics, "definition_hash": None,
              "dependency_hashes": {}, "checker_versions": {}}

    def fail(code, path, message):
        diagnostics.append(_problem(code, path, message))

    def keys(value, allowed, required, path):
        if not isinstance(value, dict):
            fail("invalid_schema", path, "expected an object")
            return False
        for key in sorted(set(value) - set(allowed), key=str):
            fail("unknown_field", f"{path}.{key}", "field is not part of sop/1")
        for key in sorted(set(required) - set(value)):
            fail("missing_field", f"{path}.{key}", "required field is missing")
        return True

    try:
        # canonical rejects non-JSON values and NaN. Roundtripping also prevents
        # accidental Python container subclasses from becoming protocol state.
        canonical(definition)
        if not isinstance(definition, dict) or any(not isinstance(k, str) for k in definition):
            raise SopError("invalid_json", "definition must be an object with string keys")
        result["definition_hash"] = digest(definition)
        catalog = registry.catalog()
        checkers = registry.checkers()
        tasks = registry.task_contracts()
        parameter_forms = registry.parameter_forms()
    except (SopError, TypeError, ValueError) as exc:
        fail(getattr(exc, "code", "invalid_definition"), "$", str(exc))
        return result
    if not keys(definition, {"schema", "id", "task", "params", "capabilities", "steps", "outputs", "checks", "recovery", "supported_rules"},
                {"schema", "id", "task", "params", "capabilities", "steps", "outputs", "checks", "recovery"}, "$"):
        return result
    if definition.get("schema") != "sop/1":
        fail("invalid_schema", "$.schema", "expected sop/1")
    if not isinstance(definition.get("id"), str) or not _NAME.fullmatch(definition["id"]):
        fail("invalid_id", "$.id", "invalid definition identifier")
    task = definition.get("task")
    contract = tasks.get(task) if isinstance(task, str) else None
    if contract is None:
        fail("unknown_task", "$.task", "task contract is not registered")
    params = definition.get("params")
    if not isinstance(params, dict):
        fail("invalid_schema", "$.params", "expected an object")
        params = {}
    valid_params = {}
    for name, schema in params.items():
        path = f"$.params.{name}"
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            fail("invalid_id", path, "invalid parameter name")
        if not keys(schema, {"type", "enum", "required"}, {"type", "required"}, path):
            continue
        kind = schema.get("type")
        if kind not in ("artifact", "string", "integer", "boolean", "object"):
            fail("invalid_type", path + ".type", "unsupported parameter type")
        if kind == "object" and (contract or {}).get("inputs", {}).get(name, {}).get("type") != "object":
            fail("invalid_type", path + ".type", "object parameters require an exact registered contract field")
        if type(schema.get("required")) is not bool:
            fail("invalid_type", path + ".required", "required must be boolean")
        if "enum" in schema:
            enum = schema["enum"]
            if kind == "object":
                fail("invalid_enum", path + ".enum", "object shape is owned by the registered parameter form")
            if not isinstance(enum, list) or not enum or any(not _is_type(item, kind) for item in enum):
                fail("invalid_enum", path + ".enum", "enum must contain values of the declared scalar type")
            elif len({canonical(item) for item in enum}) != len(enum):
                fail("invalid_enum", path + ".enum", "enum values must be unique")
        valid_params[name] = {**schema, "role": (contract or {}).get("inputs", {}).get(name, {}).get("role", name)}
    if contract:
        for name, schema in contract["inputs"].items():
            actual = valid_params.get(name)
            if not actual or actual.get("type") != schema["type"]:
                fail("task_contract", f"$.params.{name}", "task parameter missing or wrong type")
            elif schema.get("required") and actual.get("required") is not True:
                fail("task_contract", f"$.params.{name}", "task input must be required")
            if actual and "enum" in schema and (not actual.get("enum") or any(v not in schema["enum"] for v in actual["enum"])):
                fail("task_contract", f"$.params.{name}.enum", "rule domain must be explicitly bounded by the task contract")
        for name in set(params) - set(contract["inputs"]):
            fail("task_contract", f"$.params.{name}", "undeclared task parameter")
    supported = definition.get("supported_rules")
    if supported is not None:
        if not isinstance(supported, list) or not supported or any(r not in RULES for r in supported) or len(set(supported)) != len(supported):
            fail("invalid_applicability", "$.supported_rules", "unsupported or duplicate rule")
        elif not set(supported).issubset(set(valid_params.get("rule", {}).get("enum", []))):
            fail("invalid_applicability", "$.supported_rules", "applicability must be within the parameter domain")
    capabilities = definition.get("capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities):
        fail("invalid_schema", "$.capabilities", "expected capability identifier list")
        capabilities = []
    if len(capabilities) != len(set(capabilities)):
        fail("invalid_schema", "$.capabilities", "duplicate capability")
    for capability in capabilities:
        if capability not in catalog:
            fail("unknown_capability", "$.capabilities", capability)
        else:
            result["dependency_hashes"][capability] = catalog[capability]["version"]
            if catalog[capability].get("effects") != "local_artifact":
                fail("permission_denied", "$.capabilities", f"unsupported effect for {capability}")
    if contract and not set(capabilities).issubset(set(contract["capabilities"])):
        fail("permission_denied", "$.capabilities", "capability exceeds task's declared local scope")

    produced = {}

    def binding(value, schema, path, allow_missing=False):
        actual = None
        if isinstance(value, str) and value.startswith("$"):
            match = _BINDING.fullmatch(value)
            if not match:
                fail("invalid_binding", path, "bindings are $input.NAME or $steps.ID.OUTPUT")
                return
            if match.group(2):
                actual = valid_params.get(match.group(2))
            else:
                actual = produced.get(match.group(3), {}).get(match.group(4))
            if actual is None:
                fail("invalid_binding", path, "binding does not reference an available prior value")
                return
            if actual.get("type") != schema.get("type"):
                fail("binding_type", path, "source and destination types differ")
            if schema.get("type") == "artifact" and schema.get("role") and actual.get("role") != schema["role"]:
                fail("binding_role", path, "artifact does not implement the required contract output/input role")
            if schema.get("required") and not actual.get("required") and not allow_missing:
                fail("missing_dependency", path, "required operation depends on an optional parameter")
            if "enum" in schema and ("enum" not in actual or not set(actual["enum"]).issubset(set(schema["enum"]))):
                fail("binding_domain", path, "source domain exceeds the destination domain")
        else:
            if not _is_type(value, schema.get("type")):
                fail("binding_type", path, "literal has wrong type; artifact paths must be bound inputs")
            elif "enum" in schema and value not in schema["enum"]:
                fail("binding_domain", path, "literal is outside the allowed domain")

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        fail("invalid_schema", "$.steps", "expected nonempty step list")
        steps = []
    if contract and contract.get("fixed_capability"):
        if (len(steps) != 1 or not isinstance(steps[0], dict) or steps[0].get("kind") != "fixed" or
                steps[0].get("capability") != contract["fixed_capability"]):
            fail("fixed_contract", "$.steps", "this registered task requires its exact single fixed capability")
    seen = set()
    for index, step in enumerate(steps):
        path = f"$.steps[{index}]"
        if not isinstance(step, dict):
            fail("invalid_schema", path, "expected an object")
            continue
        kind = step.get("kind")
        if kind == "fixed":
            keys(step, {"id", "kind", "capability", "inputs"}, {"id", "kind", "capability", "inputs"}, path)
        elif kind in ("pi", "call"):
            keys(step, {"id", "kind", "task", "inputs"} | ({"definition"} if kind == "call" else set()), {"id", "kind", "task", "inputs"}, path)
        else:
            fail("invalid_mode", path + ".kind", "execution mode must be fixed, pi, or call")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not _NAME.fullmatch(step_id):
            fail("invalid_id", path + ".id", "invalid step identifier")
            continue
        if step_id in seen:
            fail("duplicate_step", path + ".id", "duplicate step identifier")
        seen.add(step_id)
        signature = None
        if kind == "fixed":
            capability = step.get("capability")
            signature = catalog.get(capability) if isinstance(capability, str) else None
            if signature is None:
                fail("unknown_capability", path + ".capability", "fixed capability is not registered")
            elif capability not in capabilities:
                fail("permission_denied", path + ".capability", "capability omitted from definition grant")
        elif kind in ("pi", "call"):
            subtask = step.get("task")
            signature = tasks.get(subtask) if isinstance(subtask, str) else None
            if signature is None:
                fail("unknown_task", path + ".task", "subtask contract is not registered")
            elif not set(signature["capabilities"]).issubset(set(capabilities)):
                fail("permission_denied", path, "transitive child capabilities must be declared")
            if "definition" in step and (not isinstance(step["definition"], str) or not _HASH.fullmatch(step["definition"])):
                fail("invalid_reference", path + ".definition", "expected content hash")
        inputs = step.get("inputs")
        if not isinstance(inputs, dict):
            fail("invalid_schema", path + ".inputs", "expected an object")
            inputs = {}
        if signature:
            schemas = signature["inputs"]
            for name in set(inputs) - set(schemas):
                fail("unknown_field", path + f".inputs.{name}", "input is not declared by capability/task")
            for name, schema in schemas.items():
                # A Pi task's declared information request can obtain its rule.
                permits_question = kind == "pi" and name == "rule"
                if name not in inputs:
                    if schema.get("required") and not permits_question:
                        fail("missing_dependency", path + f".inputs.{name}", "required operation input is missing")
                else:
                    binding(inputs[name], {**schema, "role": schema.get("role", name)}, path + f".inputs.{name}", permits_question)
                    if (name in ("rule", "column_map", "chunk_size") and inputs[name] != "$input." + name) or (name == "source_file" and inputs[name] != "$input.source_file" and not (isinstance(inputs[name], str) and inputs[name].startswith("$steps."))):
                        fail("contract_binding", path + f".inputs.{name}", "task inputs cannot silently replace the caller's source or declared parameters")
            produced[step_id] = {name: {"type": "artifact", "required": True, "role": signature.get("output_roles", {}).get(name, name)} for name in signature["outputs"]}
    outputs = definition.get("outputs")
    if not isinstance(outputs, dict):
        fail("invalid_schema", "$.outputs", "expected an object")
        outputs = {}
    if contract and set(outputs) != set(contract["outputs"]):
        fail("task_contract", "$.outputs", "outputs must exactly match the parent task contract")
    for name, value in outputs.items():
        binding(value, {"type": "artifact", "required": True, "role": (contract or {}).get("output_roles", {}).get(name, name)}, f"$.outputs.{name}")
    checks = definition.get("checks")
    if not isinstance(checks, list) or any(not isinstance(c, str) for c in checks):
        fail("invalid_schema", "$.checks", "expected checker identifier list")
        checks = []
    if len(checks) != len(set(checks)):
        fail("invalid_schema", "$.checks", "duplicate checker")
    if contract and task not in checks:
        fail("missing_checker", "$.checks", "parent task's independent checker is required")
    if contract and checks != [task]:
        fail("task_contract", "$.checks", "the declared checker must exactly match the registered task verifier")
    for checker in checks:
        if checker not in checkers:
            fail("unknown_checker", "$.checks", checker)
        else:
            result["checker_versions"][checker] = checkers[checker]
    recovery = definition.get("recovery")
    if not isinstance(recovery, list):
        fail("invalid_schema", "$.recovery", "expected recovery rule list")
        recovery = []
    recovery_ids, selectors = set(), set()
    for index, rule in enumerate(recovery):
        path = f"$.recovery[{index}]"
        parameter_rule = isinstance(rule, dict) and rule.get("phase") == "parameter_validation"
        recovery_fields = {"id", "phase", "code", "action", "max_attempts"} | ({"fields"} if parameter_rule else set())
        if not keys(rule, recovery_fields, recovery_fields, path):
            continue
        rid = rule.get("id")
        if not isinstance(rid, str) or not _NAME.fullmatch(rid) or rid in recovery_ids:
            fail("invalid_recovery", path + ".id", "invalid or duplicate recovery ID")
        else:
            recovery_ids.add(rid)
        phase, code, action = rule.get("phase"), rule.get("code"), rule.get("action")
        if phase not in ("execution", "verification", "parameter_validation") or not isinstance(code, str) or not _NAME.fullmatch(code):
            fail("invalid_recovery", path, "invalid phase or error code")
        if action != {"execution": "retry", "verification": "recheck",
                      "parameter_validation": "request_parameter_proposal"}.get(phase):
            fail("invalid_recovery", path + ".action", "action must match the registered recovery phase")
        if parameter_rule:
            form = parameter_forms.get(task)
            if (not isinstance(form, dict) or code != "invalid_value" or rule.get("fields") != ["chunk_size"] or
                    form.get("fields", {}).get("chunk_size", {}).get("type") != "integer" or
                    (contract or {}).get("inputs", {}).get("chunk_size", {}).get("type") != "integer"):
                fail("invalid_recovery", path, "parameter recovery may request only chunk_size on its registered form")
        if code in ("verification_failed", "check_failed", "effect_unknown", "permission_denied", "dependency_drift") or code == "*":
            fail("invalid_recovery", path + ".code", "this failure cannot authorize recovery")
        if (type(rule.get("max_attempts")) is not int or rule["max_attempts"] < 1 or
                (not parameter_rule and rule["max_attempts"] > 100)):
            fail("invalid_recovery", path + ".max_attempts", "expected a positive bounded recovery count")
        selector = (str(phase), str(code))
        if selector in selectors:
            fail("ambiguous_recovery", path, "recovery selector must be unique")
        selectors.add(selector)
    result["passed"] = not diagnostics
    return result


def clean_definition(rule=None):
    if rule is not None and rule not in RULES:
        raise SopError("invalid_rule", "unsupported rule")
    definition = {"schema": "sop/1", "id": "orders-clean", "task": "orders.clean",
                  "params": {"source_file": {"type": "artifact", "required": True},
                             "rule": {"type": "string", "enum": [rule] if rule else RULES[:], "required": True}},
                  "capabilities": CAPABILITIES[1:3],
                  "steps": [{"id": "normalize", "kind": "fixed", "capability": "orders.normalize", "inputs": {"source_file": "$input.source_file"}},
                            {"id": "deduplicate", "kind": "fixed", "capability": "orders.deduplicate", "inputs": {"normalized": "$steps.normalize.normalized", "rule": "$input.rule"}}],
                  "outputs": {"cleaned": "$steps.deduplicate.cleaned"}, "checks": ["orders.clean"], "recovery": []}
    if rule:
        definition["supported_rules"] = [rule]
    return definition


def root_definition(mode="pi"):
    if mode not in ("pi", "fixed"):
        raise SopError("invalid_mode", "root mode must be pi or fixed")
    definition = {"schema": "sop/1", "id": "orders-report", "task": "orders.report",
                  "params": {"source_file": {"type": "artifact", "required": True},
                             "rule": {"type": "string", "enum": RULES[:], "required": mode == "fixed"}},
                  "capabilities": CAPABILITIES[:],
                  "steps": [{"id": "profile", "kind": "fixed", "capability": "orders.profile", "inputs": {"source_file": "$input.source_file"}}],
                  "outputs": {}, "checks": ["orders.report"], "recovery": []}
    if mode == "pi":
        definition["steps"].append({"id": "clean", "kind": "pi", "task": "orders.clean", "inputs": {"source_file": "$input.source_file", "rule": "$input.rule"}})
        clean_binding = "$steps.clean.cleaned"
    else:
        definition["steps"].extend(clean_definition()["steps"])
        clean_binding = "$steps.deduplicate.cleaned"
    definition["steps"].append({"id": "summary", "kind": "fixed", "capability": "orders.summarize", "inputs": {"cleaned": clean_binding, "source_file": "$input.source_file"}})
    definition["outputs"] = {"cleaned": clean_binding, "summary": "$steps.summary.summary", "quality": "$steps.summary.quality"}
    return definition


def prepare_definition():
    return {"schema":"sop/1","id":"orders-prepare","task":"orders.prepare",
            "params":{"source_file":{"type":"artifact","required":True}},
            "capabilities":["orders.prepare"],
            "steps":[{"id":"convert","kind":"fixed","capability":"orders.prepare",
                      "inputs":{"source_file":"$input.source_file"}}],
            "outputs":{"prepared":"$steps.convert.prepared","mapping":"$steps.convert.mapping"},
            "checks":["orders.prepare"],"recovery":[]}


def configured_prepare_definition():
    return {"schema": "sop/1", "id": "orders-prepare-configured", "task": "orders.prepare_configured",
            "params": {"source_file": {"type": "artifact", "required": True},
                       "column_map": {"type": "object", "required": True},
                       "chunk_size": {"type": "integer", "required": True}},
            "capabilities": ["orders.prepare_configured"],
            "steps": [{"id": "convert", "kind": "fixed", "capability": "orders.prepare_configured",
                       "inputs": {"source_file": "$input.source_file", "column_map": "$input.column_map",
                                  "chunk_size": "$input.chunk_size"}}],
            "outputs": {"prepared": "$steps.convert.prepared", "mapping": "$steps.convert.mapping"},
            "checks": ["orders.prepare_configured"], "recovery": []}


def json_report_definition(mode="pi"):
    definition=root_definition(mode)
    definition.update(id="orders-from-json",task="orders.from_json")
    definition["capabilities"]=["orders.prepare",*definition["capabilities"]]
    definition["checks"]=["orders.from_json"]
    for step in definition["steps"]:
        if step["inputs"].get("source_file")=="$input.source_file":
            step["inputs"]["source_file"]="$steps.prepare.prepared"
    definition["steps"].insert(0,{"id":"prepare","kind":"call","task":"orders.prepare",
                                  "inputs":{"source_file":"$input.source_file"}})
    definition["outputs"].update(prepared="$steps.prepare.prepared",mapping="$steps.prepare.mapping")
    return definition


class Library:
    """Explicit candidate library. Adding/reusing never publishes a candidate."""
    def __init__(self, definitions=()):
        self.definitions = []
        self._origins = {}
        for definition in definitions:
            self.add(definition)

    def add(self, definition, origin="existing"):
        if origin not in ("existing", "generated", "candidate_reuse"):
            raise SopError("invalid_origin", origin)
        identity = digest(definition)
        if identity not in self._origins:
            self.definitions.append(deepcopy(definition))
            self._origins[identity] = origin
        return identity

    def get(self, identity):
        for definition in self.definitions:
            if digest(definition) == identity:
                return deepcopy(definition)
        raise SopError("definition_missing", str(identity))

    def matching(self, task, inputs):
        return [deepcopy(d) for d in self.definitions if d.get("task") == task and _applicable(d, inputs)]


def _applicable(definition, inputs):
    rule = inputs.get("rule")
    if rule is not None:
        supported = definition.get("supported_rules", definition.get("params", {}).get("rule", {}).get("enum", []))
        if rule not in supported:
            return False
    return True


def prepare_method(task, inputs, library, registry, planner=None):
    tasks = registry.task_contracts()
    if task not in tasks:
        raise SopError("unknown_task", str(task))
    if not isinstance(inputs, dict) or ("rule" in tasks[task]["inputs"] and inputs.get("rule") not in RULES):
        raise SopError("missing_information", "method selection needs an explicit supported rule")
    selection = []
    for definition in library.definitions:
        if definition.get("task") != task:
            continue
        identity = digest(definition)
        report = validate_definition(definition, registry)
        if not report["passed"]:
            selection.append({"definition_hash": identity, "accepted": False, "reason": "definition_invalid", "diagnostics": report["diagnostics"]})
            continue
        if not _applicable(definition, inputs):
            selection.append({"definition_hash": identity, "accepted": False, "reason": "rule_not_supported"})
            continue
        selection.append({"definition_hash": identity, "accepted": True, "reason": "applicable_existing"})
        origin = "candidate_reuse" if library._origins.get(identity) in ("generated", "candidate_reuse") else "existing"
        return {"definition": deepcopy(definition), "origin": origin, "selection": selection}
    if planner is None:
        raise SopError("capability_missing", "no applicable method and no planner is available")
    context = {"task": task, "rule": inputs.get("rule"), "catalog": registry.catalog(),
               "contract": deepcopy(tasks[task]), "selection": deepcopy(selection)}
    definition = planner.plan(context)
    report = validate_definition(definition, registry)
    if not report["passed"]:
        raise SopError("definition_invalid", canonical(report["diagnostics"]))
    if definition["task"] != task or not _applicable(definition, inputs):
        raise SopError("contract_mismatch", "generated method does not satisfy the requested task and rule")
    selection.append({"definition_hash": report["definition_hash"], "accepted": True, "reason": "generated_by_current_planner"})
    return {"definition": deepcopy(definition), "origin": "generated", "selection": selection}


# Exact, documented scoped text. Unknown nonblank lines are always retained and
# block automatic delivery, including a single appended upload/delete request.
_SCOPED = {
    "# 本地订单清洗与客户汇总请求": ("background", "title"),
    "请读取给定的订单 CSV，在本地生成清洗后的订单表、按客户汇总表，以及一份数据质量统计。输入可能包含订单修订记录。同一订单在汇总中只能计算一次；如果决定保留哪条记录的业务口径不明确，请先提出结构化问题，不要自行猜测。": ("requirement", "deliverables"),
    "要求如下：": ("background", "heading"),
    "- 输入字段为 `order_id,customer_id,revision,gross_cents,refund_cents`。`revision` 是整数修订号；金额是整数分，计算中不得使用浮点金额或四舍五入。": ("requirement", "input"),
    "- 清理 `customer_id` 首尾空白，不改变标识的其他字符。金额、订单 ID 和修订号不能因清洗被随意改写。": ("requirement", "normalize"),
    "- 同一个 `order_id` 的客户归属必须一致；规范化后仍对应多个 `customer_id` 时拒绝处理，不能替用户选择客户。": ("requirement", "cross_customer"),
    "- 清洗表每个订单保留一行，新增 `net_cents = gross_cents - refund_cents`，按 `order_id` 升序输出。": ("requirement", "clean"),
    "- 汇总表按 `customer_id` 分组，输出订单数、总原始金额、总退款金额和总净额，并按 `customer_id` 升序输出。": ("requirement", "summary"),
    "- 质量统计报告输入行数、保留订单数、移除行数，以及清洗后订单的 gross/refund/net 金额合计。三份结果必须互相核对。": ("requirement", "quality"),
    "- 请求尚未确定同一订单多条修订记录的保留规则。请识别并提出该信息请求，得到回答后再执行清洗与汇总。": ("requirement", "ambiguity"),
    "- 本次范围是本地读取和写出指定结果，不涉及 OSS、网络、上传或修改输入数据。": ("requirement", "permissions"),
    "该请求适用于 `batch-01/input.csv` 或 `batch-02/input.csv`。每次运行只处理指定的一批，不合并两批。这里没有指定使用已有 SOP 还是生成新方法；两条用例应使用相同输入、业务要求和回答。": ("requirement", "reuse"),
    "去重规则：highest_revision。": ("requirement", "rule_highest_revision"),
    "去重规则：first。": ("requirement", "rule_first"),
    "执行模式：fixed。": ("requirement", "fixed"),
    "执行模式：pi。": ("requirement", "pi"),
}
_REQUIRED_SCOPE = {"input", "normalize", "cross_customer", "clean", "summary", "quality", "permissions"}


class Authoring:
    """Persistent authoring with explicit evidence boundaries and no execution."""
    def __init__(self, data_dir, registry, model=None, max_model_calls=1):
        if type(max_model_calls) is not int or max_model_calls < 0:
            raise SopError("invalid_budget", "max_model_calls must be a nonnegative integer")
        self.max_model_calls = max_model_calls
        self.data_dir = Path(data_dir)
        self.base = self.data_dir / "authoring"
        self.registry = registry
        self.model = model
        self.base.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self):
        with (self.base / ".lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, draft_id):
        if not isinstance(draft_id, str) or not re.fullmatch(r"draft-[0-9a-f]{32}", draft_id):
            raise SopError("draft_missing", "invalid draft identity")
        return self.base / "drafts" / (draft_id + ".json")

    def _save(self, draft):
        try:
            write_json(self._path(draft["draft_id"]), draft)
        except OSError as exc:
            raise SopError("storage_failure", str(exc)) from exc

    def _read(self, draft_id):
        path = self._path(draft_id)
        if not path.is_file():
            raise SopError("draft_missing", draft_id)
        return read_json(path)

    def submit(self, source_path, proposal=None):
        source_path = Path(source_path).absolute()
        if source_path.is_symlink() or not source_path.is_file():
            raise SopError("source_invalid", "source must be a regular, non-symlink text file")
        try:
            raw = source_path.read_bytes()
            source = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SopError("source_invalid", str(exc)) from exc
        import hashlib
        source_hash = hashlib.sha256(raw).hexdigest()
        draft_id = "draft-" + uuid.uuid4().hex
        snapshot = self.base / "sources" / (source_hash + ".json")
        with self._lock():
            try:
                write_json(snapshot, {"source_hash": source_hash, "text": source})
            except OSError as exc:
                raise SopError("storage_failure", str(exc)) from exc
            requirements, lines, categories = [], [], set()
            for number, text in enumerate(source.splitlines(), 1):
                stripped = text.strip()
                classification, category = _SCOPED.get(stripped, ("uninterpreted", "unsupported")) if stripped else ("blank", "blank")
                row = {"line": number, "text": text, "classification": classification, "requirement_ids": []}
                if classification in ("requirement", "uninterpreted"):
                    rid = f"R{len(requirements) + 1:03d}"
                    row["requirement_ids"] = [rid]
                    requirements.append({"id": rid, "source": {"line": number, "text": text}, "meaning": category,
                                         "status": "unsupported" if classification == "uninterpreted" else "pending",
                                         "targets": [], "checks": ["orders.report"] if classification == "requirement" else [],
                                         "revision_source": "source"})
                categories.add(category)
                lines.append(row)
            draft = {"schema": "draft/1", "draft_id": draft_id, "revision": 1, "state": "authoring",
                     "source": {"path": str(source_path), "snapshot": str(snapshot), "hash": source_hash},
                     "source_hash": source_hash, "source_lines": lines, "requirements": requirements,
                     "requirement_map": {"forward": {}, "reverse": {}}, "questions": [], "answers": {},
                     "definition": None, "proposal": deepcopy(proposal), "report": None, "report_history": [],
                     "diagnostics": [], "publication": "unpublished", "trials": "not_run", "business_calls": 0,
                     "usage": {"model_calls": 0}, "limits": {"max_model_calls": self.max_model_calls},
                     "semantic_evidence": {"method": "scoped_orders_fixture_parser", "general_nl_validation": "not_run",
                                           "business_semantics": "independent_runtime_checker_required"},
                     "receipts": {}, "mode": "fixed" if "fixed" in categories else "pi"}
            unknown = [r for r in requirements if r["status"] == "unsupported"]
            if unknown and self.model is not None and proposal is None:
                if draft["usage"]["model_calls"] >= draft["limits"]["max_model_calls"]:
                    draft["diagnostics"].append(_problem("budget_exhausted", "$.limits.max_model_calls", "authoring model-call budget is exhausted"))
                else:
                    # Persist the reservation before crossing the backend boundary.
                    # An interrupted attempt consumes its reservation and is never
                    # silently retried by get(); there is no automatic revision loop.
                    draft["usage"]["model_calls"] += 1
                    draft["model_attempt"] = {"state": "reserved", "number": draft["usage"]["model_calls"]}
                    self._save(draft)
                    try:
                        draft["proposal"] = self.model.author({"source": source, "source_hash": source_hash,
                                                               "catalog": self.registry.catalog(), "scope": "orders.report",
                                                               "limits": deepcopy(draft["limits"]), "usage": deepcopy(draft["usage"])})
                        canonical(draft["proposal"])
                        draft["semantic_evidence"]["method"] = "injected_model_unreviewed"
                        draft["model_attempt"]["state"] = "completed"
                    except Exception as exc:
                        draft["model_attempt"]["state"] = "failed"
                        draft["diagnostics"].append(_problem(getattr(exc, "code", "model_failure"), "$.model", str(exc)))
            for requirement in unknown:
                draft["diagnostics"].append(_problem("unsupported_requirement", f"source:{requirement['source']['line']}", requirement["source"]["text"]))
            missing = sorted(_REQUIRED_SCOPE - categories)
            if missing:
                draft["diagnostics"].append(_problem("scope_incomplete", "$.source", "missing explicit scoped requirements: " + ", ".join(missing)))
            if "fixed" in categories and "pi" in categories:
                draft["diagnostics"].append(_problem("conflicting_requirements", "$.source", "conflicting execution modes"))
            known_rules = [r for r in RULES if "rule_" + r in categories]
            if len(known_rules) > 1:
                draft["diagnostics"].append(_problem("conflicting_requirements", "$.source", "conflicting deduplication rules"))
            if draft["diagnostics"]:
                draft["state"] = "stopped"
            elif known_rules:
                draft["answers"]["rule"] = {"value": known_rules[0], "source": "source", "source_hash": source_hash}
                self._compile(draft)
            else:
                question = {"request_id": "dedup_policy", "field": "rule", "status": "open",
                            "reason": "同一订单多条修订应保留哪一条？请选择明确规则；不会采用候选默认值。",
                            "schema": {"type": "string", "enum": RULES[:]},
                            "options": {"highest_revision": "最大整数 revision；最高修订内容相同合并，冲突拒绝；任一修订跨客户拒绝。",
                                        "first": "规范化后按输入行序保留第一条；任一修订跨客户拒绝。"},
                            "requirements": [r["id"] for r in requirements if r["meaning"] in ("ambiguity", "clean", "deliverables")]}
                draft["questions"] = [question]
                draft["state"] = "waiting_info"
            self._save(draft)
            return deepcopy(draft)

    def _compile(self, draft):
        draft["state"] = "checking"
        proposal = draft.get("proposal")
        if proposal is None:
            definition = root_definition(draft["mode"])
            if not draft.get("rule_at_runtime"):
                definition["params"]["rule"]["enum"] = [draft["answers"]["rule"]["value"]]
                definition["supported_rules"] = [draft["answers"]["rule"]["value"]]
        elif isinstance(proposal, dict) and proposal.get("schema") == "sop/1":
            definition = deepcopy(proposal)
        elif isinstance(proposal, dict) and "definition" in proposal:
            # Requirement interpretations remain tied to preserved source lines;
            # a generated correspondence table cannot delete unknown source text.
            if set(proposal) - {"definition", "requirements", "source_annotations", "semantics"}:
                draft["diagnostics"].append(_problem("unknown_field", "$.proposal", "unknown proposal field"))
            definition = deepcopy(proposal["definition"])
        else:
            draft["diagnostics"].append(_problem("invalid_proposal", "$.proposal", "expected definition or structured proposal"))
            definition = {}
        draft["definition"] = definition
        report = validate_definition(definition, self.registry)
        if report["passed"]:
            rule = draft["answers"].get("rule", {}).get("value")
            if (not draft.get("rule_at_runtime") and (not _applicable(definition, {"rule": rule}) or definition["params"]["rule"]["enum"] != [rule])) or (draft.get("rule_at_runtime") and definition["params"]["rule"]["enum"] != RULES):
                report["diagnostics"].append(_problem("requirement_mismatch", "$.params.rule", "candidate must preserve the explicit business rule from the effective answer"))
            if definition["task"] != "orders.report":
                report["diagnostics"].append(_problem("requirement_mismatch", "$.task", "source requires the complete report task"))
            if draft["mode"] == "fixed" and any(step["kind"] != "fixed" for step in definition["steps"]):
                report["diagnostics"].append(_problem("fixed_mode_violation", "$.steps", "source requires fixed execution without a model"))
        self._map(draft)
        if isinstance(proposal, dict) and "definition" in proposal:
            if "source_annotations" in proposal and proposal["source_annotations"] != draft["source_lines"]:
                report["diagnostics"].append(_problem("provenance_mismatch", "$.proposal.source_annotations", "annotations must account for exactly the preserved source lines"))
            if "requirements" in proposal:
                supplied = proposal["requirements"]
                expected = draft["requirements"]
                fields = ("id", "source", "meaning", "targets", "checks")
                if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied) or len(supplied) != len(expected):
                    report["diagnostics"].append(_problem("provenance_mismatch", "$.proposal.requirements", "proposal cannot add or omit preserved source requirements"))
                elif [{key: item.get(key) for key in fields} for item in supplied] != [{key: item.get(key) for key in fields} for item in expected]:
                    report["diagnostics"].append(_problem("provenance_mismatch", "$.proposal.requirements", "proposal interpretations and targets must match the supported source contract"))
        for requirement in draft["requirements"]:
            if not requirement["targets"]:
                report["diagnostics"].append(_problem("requirement_unmapped", f"source:{requirement['source']['line']}", requirement["id"]))
        report["diagnostics"].extend(draft["diagnostics"])
        # A reviewer can navigate candidate diagnostics back to the original
        # requirement lines, even when compilation fails before graph admission.
        for diagnostic in report["diagnostics"]:
            path = diagnostic.get("path", "")
            target = path.removeprefix("$.")
            match = re.match(r"steps\[(\d+)\]", target)
            if match and isinstance(definition, dict):
                candidate_steps = definition.get("steps", [])
                index = int(match.group(1))
                if isinstance(candidate_steps, list) and index < len(candidate_steps) and isinstance(candidate_steps[index], dict):
                    target = "steps." + str(candidate_steps[index].get("id", ""))
            related = set()
            for location, requirement_ids in draft["requirement_map"]["reverse"].items():
                if target == location or target.startswith(location + ".") or location.startswith(target + "."):
                    related.update(requirement_ids)
            if diagnostic["code"] in ("permission_denied", "unknown_capability", "unjustified_action"):
                related.update(r["id"] for r in draft["requirements"] if r["meaning"] == "permissions")
            diagnostic["requirement_ids"] = sorted(related)
            diagnostic["source_lines"] = sorted(r["source"]["line"] for r in draft["requirements"] if r["id"] in related)
        report["passed"] = not report["diagnostics"]
        report.update({"source_hash": draft["source_hash"], "answers_hash": digest(draft["answers"]),
                       "requirements_hash": digest(draft["requirements"]), "source_lines_hash": digest(draft["source_lines"]),
                       "requirement_map_hash": digest(draft["requirement_map"]), "policy_version": "scoped-orders-authoring/1",
                       "compiler_version": file_digest(Path(__file__)),
                       "applicable": True, "semantic_evidence": deepcopy(draft["semantic_evidence"]),
                       "trials": "not_run", "execution_authorized": False, "result_verified": False, "published": False})
        draft["report"] = report
        draft["state"] = "delivered" if report["passed"] else "stopped"

    def _map(self, draft):
        definition = draft["definition"] if isinstance(draft["definition"], dict) else {}
        steps = definition.get("steps", [])
        if not isinstance(steps, list):
            steps = []
        clean = [f"steps.{s['id']}" for s in steps if isinstance(s, dict) and isinstance(s.get("id"), str)
                 and (s.get("capability") in ("orders.normalize", "orders.deduplicate") or s.get("task") == "orders.clean")]
        summary = [f"steps.{s['id']}" for s in steps if isinstance(s, dict) and isinstance(s.get("id"), str) and s.get("capability") == "orders.summarize"]
        profile = [f"steps.{s['id']}" for s in steps if isinstance(s, dict) and isinstance(s.get("id"), str) and s.get("capability") == "orders.profile"]
        mappings = {"deliverables": ["outputs", "checks.orders.report"], "input": ["params.source_file", "checks.orders.report"] + profile,
                    "normalize": clean, "cross_customer": clean + ["checks.orders.report"], "clean": clean + ["outputs.cleaned"],
                    "summary": summary + ["outputs.summary"], "quality": summary + ["outputs.quality", "checks.orders.report"],
                    "ambiguity": ["params.rule", "answers.rule"], "permissions": ["capabilities", "checks.orders.report"],
                    "reuse": ["params.source_file"], "fixed": [f"steps.{s['id']}" for s in steps if isinstance(s, dict) and "id" in s],
                    "pi": [f"steps.{s['id']}" for s in steps if isinstance(s, dict) and s.get("kind") == "pi" and "id" in s],
                    "rule_highest_revision": ["params.rule", "answers.rule", "checks.orders.report"],
                    "rule_first": ["params.rule", "answers.rule", "checks.orders.report"],
                    "rule_runtime": ["params.rule", "checks.orders.report"]}
        forward, reverse = {}, {}
        for requirement in draft["requirements"]:
            targets = mappings.get(requirement["meaning"], [])
            requirement["targets"] = targets
            requirement["status"] = "mapped" if targets else "unsupported"
            if requirement["meaning"] == "ambiguity" and draft["answers"]:
                requirement["revision_source"] = "answers.rule"
            forward[requirement["id"]] = targets[:]
            for target in targets:
                reverse.setdefault(target, []).append(requirement["id"])
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("id"), str) and f"steps.{step['id']}" not in reverse:
                draft["diagnostics"].append(_problem("unjustified_action", f"steps.{step['id']}", "business action has no original requirement"))
        draft["requirement_map"] = {"forward": forward, "reverse": reverse}

    def _invalidate(self, draft):
        report = draft.get("report")
        if not report or not report.get("applicable"):
            return False
        reasons = []
        try:
            if file_digest(Path(draft["source"]["path"])) != report["source_hash"]:
                reasons.append("source_changed")
            snapshot = read_json(draft["source"]["snapshot"])
            import hashlib
            if hashlib.sha256(snapshot["text"].encode()).hexdigest() != report["source_hash"]:
                reasons.append("source_snapshot_changed")
        except (OSError, SopError, KeyError, TypeError):
            reasons.append("source_missing")
        if digest(draft["answers"]) != report["answers_hash"]:
            reasons.append("answers_changed")
        if (digest(draft["requirements"]) != report["requirements_hash"]
                or digest(draft["source_lines"]) != report["source_lines_hash"]
                or digest(draft["requirement_map"]) != report["requirement_map_hash"]):
            reasons.append("requirements_changed")
        if file_digest(Path(__file__)) != report["compiler_version"]:
            reasons.append("compiler_changed")
        current = validate_definition(draft["definition"], self.registry)
        if current["definition_hash"] != report["definition_hash"]:
            reasons.append("definition_changed")
        if current["dependency_hashes"] != report["dependency_hashes"] or current["checker_versions"] != report["checker_versions"]:
            reasons.append("dependency_changed")
        if not current["passed"]:
            reasons.append("definition_invalid")
        if reasons:
            draft["report_history"].append(deepcopy(report))
            draft["report"] = {**deepcopy(report), "passed": False, "applicable": False,
                               "invalidated_by": sorted(set(reasons)), "diagnostics": report["diagnostics"] +
                               [_problem("stale_report", "$.report", ", ".join(sorted(set(reasons))))]}
            draft["state"] = "stopped"
            draft["revision"] += 1
            return True
        return False

    def get(self, draft_id):
        if type(self) is Authoring and self._read(draft_id).get("authoring_mode") == "semantic":
            from .skill_authoring import SemanticAuthoring
            return SemanticAuthoring(self.data_dir, self.registry).get(draft_id)
        with self._lock():
            draft = self._read(draft_id)
            if self._invalidate(draft):
                self._save(draft)
            return deepcopy(draft)

    def answer(self, draft_id, request_id, value, message_id, revision):
        if type(self) is Authoring and self._read(draft_id).get("authoring_mode") == "semantic":
            from .skill_authoring import SemanticAuthoring
            return SemanticAuthoring(self.data_dir, self.registry).answer(draft_id, request_id, value, message_id, revision)
        if not isinstance(message_id, str) or not message_id:
            raise SopError("invalid_message", "message ID is required")
        payload_hash = digest({"draft_id": draft_id, "request_id": request_id, "value": value, "revision": revision})
        with self._lock():
            draft = self._read(draft_id)
            receipt = draft["receipts"].get(message_id)
            if receipt:
                if receipt["payload_hash"] != payload_hash:
                    raise SopError("message_conflict", "message ID was already used with another payload")
                return deepcopy(receipt["response"])
            try:
                current_hash = file_digest(Path(draft["source"]["path"]))
            except OSError:
                current_hash = None
            if current_hash != draft["source_hash"]:
                draft["state"] = "stopped"
                draft["revision"] += 1
                draft["diagnostics"].append(_problem("source_changed", "$.source", "source changed while awaiting the answer; resubmit the preserved requirements"))
                self._save(draft)
                raise SopError("source_changed", "source changed after question creation")
            if type(revision) is not int or revision != draft["revision"]:
                raise SopError("stale_revision", "answer revision does not match the current draft")
            question = next((q for q in draft["questions"] if q["request_id"] == request_id and q["status"] == "open"), None)
            if draft["state"] != "waiting_info" or question is None:
                raise SopError("question_mismatch", "answer does not belong to an open question")
            rule = self._answer_rule(value)
            draft["answers"]["rule"] = {"value": rule, "source": "answer", "request_id": request_id,
                                          "message_id": message_id, "answer": deepcopy(value), "revision": revision}
            question["status"] = "answered"
            draft["revision"] += 1
            self._compile(draft)
            response = deepcopy(draft)
            # Store the original response without recursively embedding receipts.
            response["receipts"] = {}
            draft["receipts"][message_id] = {"payload_hash": payload_hash, "response": response}
            self._save(draft)
            return deepcopy(response)

    @staticmethod
    def _answer_rule(value):
        if isinstance(value, str) and value in RULES:
            return value
        if isinstance(value, dict) and set(value) == {"rule"} and value["rule"] in RULES:
            return value["rule"]
        if isinstance(value, dict) and "answer" in value:
            if set(value) - {"question_id", "answer", "explanation_zh", "fixture_only"} or value.get("question_id") != "dedup_policy":
                raise SopError("invalid_answer", "unsupported answer envelope")
            value = value["answer"]
        expected = {"group_by": "order_id", "keep": "highest_revision", "revision_order": "integer_numeric",
                    "customer_normalization": "trim_leading_and_trailing_whitespace",
                    "cross_customer_policy": "reject_if_any_revision_has_a_different_normalized_customer",
                    "highest_revision_tie_policy": {"identical_normalized_records": "retain_one", "conflicting_normalized_records": "reject",
                                                    "comparison_fields": ["customer_id", "gross_cents", "refund_cents"]}}
        if value == expected:
            return "highest_revision"
        raise SopError("invalid_answer", "answer must select a displayed rule without changing its required checks")
