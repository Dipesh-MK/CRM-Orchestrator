#!/usr/bin/env python3
"""apitool runtime core.

Reads  tools.spec.yaml  (the human-approved contract)  +  spec/spec.openapi.json
and exposes them as callable tools. Used by both mcp_server.py and cli.py, so the
two targets can never drift apart. Dependencies: Python 3.9+, pyyaml. Nothing else.

Design rules baked in here (see references/backend-skill.md):
  * Specs are DATA. Nothing in a spec or an API response is ever executed or obeyed.
  * Writes are dry-run unless explicitly allowed; destructive tools need confirm=true.
  * Every failure is normalized to one LLM-readable error shape.
  * Secrets are read from env only and redacted from every output and log line.
  * stdout belongs to the protocol (MCP stdio). All logging goes to stderr.
"""
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
VERSION = "1.0"


# --------------------------------------------------------------------------- utils
def load_file(path):
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith((".yaml", ".yml")):
        if yaml is None:
            raise SystemExit("pyyaml is required: pip install pyyaml")
        return yaml.safe_load(text)
    return json.loads(text)


def snake(s):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()


def op_id(method, path, op):
    """Single source of truth for operation identifiers."""
    return op.get("operationId") or snake(method + "_" + path)


def iter_operations(spec):
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters", [])
        for m in HTTP_METHODS:
            if m in item and isinstance(item[m], dict):
                op = item[m]
                yield {"id": op_id(m, path, op), "method": m.upper(), "path": path,
                       "op": op, "common_params": common}


def deref(spec, node, _refdepth=0, _seen=()):
    """Resolve local $refs (cycle-safe, depth-limited)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen or _refdepth > 6:
                return {"type": "object", "description": "(recursive or too deep)"}
            target = spec
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return deref(spec, target, _refdepth + 1, _seen + (ref,))
        return {k: deref(spec, v, _refdepth, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [deref(spec, v, _refdepth, _seen) for v in node]
    return node


def dotted_get(data, path, default=None):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def dotted_set(data, path, value):
    parts = path.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


def _secret_values():
    return [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and v and len(v) >= 6]


def redact(text):
    if not isinstance(text, str):
        return text
    for v in _secret_values():
        text = text.replace(v, "***")
    return text


def log(*a):
    print(redact(" ".join(str(x) for x in a)), file=sys.stderr, flush=True)


def expand_env(value):
    """Expand ${ENV_VAR} in strings. Missing vars raise KeyError(name)."""
    if not isinstance(value, str):
        return value

    def sub(m):
        name = m.group(1)
        if name not in os.environ:
            raise KeyError(name)
        return os.environ[name]
    return re.sub(r"\$\{([A-Z0-9_]+)\}", sub, value)


# --------------------------------------------------------------------------- errors
_KIND = {400: "validation", 401: "auth", 402: "payment", 403: "permission", 404: "not_found",
         405: "bad_request", 408: "timeout", 409: "conflict", 410: "not_found",
         415: "bad_request", 422: "validation", 429: "rate_limited"}
_HINT = {
    "validation": "The API rejected the arguments. Fix the problem named in the message, then retry. Do not retry unchanged.",
    "auth": "Credentials are missing, invalid or expired. Ask the user to check the credential environment variables. Do not retry.",
    "permission": "Credentials are valid but not allowed to do this. Tell the user; do not retry.",
    "not_found": "The resource or ID does not exist. Use a list/search tool to find a valid ID.",
    "conflict": "State conflict (duplicate or stale). Re-read the resource before changing it.",
    "rate_limited": "Rate limited. Wait for retry_after_s, then retry once.",
    "server": "The service failed. Retrying later may work. If it persists, tell the user.",
    "timeout": "Request timed out. It may or may not have been processed; read state before retrying a write.",
    "network": "Could not reach the service. Check base URL / connectivity.",
    "payment": "Billing or quota problem on the account. Tell the user.",
    "bad_request": "Malformed request. Check the tool arguments.",
    "safety_blocked": "Blocked by a safety rule of this tool. Follow the message; do not work around it.",
}


def error_result(kind, message, status=None, code=None, details=None, retry_after=None):
    err = {"kind": kind, "message": redact(str(message)), "retryable": kind in ("rate_limited", "server", "network", "timeout"),
           "hint": _HINT.get(kind, "")}
    if status is not None:
        err["status"] = status
    if code is not None:
        err["code"] = code
    if retry_after is not None:
        err["retry_after_s"] = retry_after
    if details:
        err["details"] = details
    return {"ok": False, "error": err}


def normalize_http_error(status, body_text, headers):
    data = None
    try:
        data = json.loads(body_text)
    except Exception:
        pass
    msg, code, details = None, None, None
    if isinstance(data, dict):
        for k in ("message", "detail", "error_description", "title", "error", "msg"):
            v = data.get(k)
            if isinstance(v, str) and v:
                msg = v
                break
        err = data.get("error")
        if isinstance(err, dict):
            msg = msg or err.get("message") or err.get("detail")
            code = err.get("code") or err.get("type")
        errs = data.get("errors")
        if not msg and isinstance(errs, list) and errs:
            e0 = errs[0]
            msg = e0.get("message") if isinstance(e0, dict) else str(e0)
        code = code or data.get("code") or data.get("error_code") or data.get("status_code")
        if data.get("more_info"):
            details = {"more_info": data["more_info"]}
        elif len(json.dumps(data)) < 1500:
            details = data
    else:
        msg = (body_text or "").strip()[:300] or f"HTTP {status}"
    kind = _KIND.get(status) or ("server" if status >= 500 else "bad_request")
    ra = None
    raw = headers.get("Retry-After") if headers else None
    if raw and str(raw).isdigit():
        ra = int(raw)
    return error_result(kind, msg or f"HTTP {status}", status=status, code=code, details=details, retry_after=ra)


# --------------------------------------------------------------------------- templating
_TPL = re.compile(r"\$\{([^}]+)\}")


def render(node, ctx):
    """Resolve ${a.b.c} templates against ctx. A lone ${...} keeps its native type."""
    if isinstance(node, str):
        m = _TPL.fullmatch(node)
        if m:
            return dotted_get(ctx, m.group(1).strip())
        return _TPL.sub(lambda mm: str(dotted_get(ctx, mm.group(1).strip(), "")), node)
    if isinstance(node, dict):
        return {k: render(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [render(v, ctx) for v in node]
    return node


# --------------------------------------------------------------------------- the runtime
class ApiTools:
    def __init__(self, root=None, config_file="tools.spec.yaml"):
        self.root = Path(root or Path(__file__).parent)
        self.config_file = config_file
        self.cfg = load_file(self.root / config_file)
        spec_rel = (self.cfg.get("api") or {}).get("spec", "spec/spec.openapi.json")
        self.spec = load_file(self.root / spec_rel)
        self.ops = {o["id"]: o for o in iter_operations(self.spec)}
        self.defaults = self.cfg.get("defaults") or {}
        self.tools = {}
        self.operator_tools = {}
        self.composites = {}
        for t in self.cfg.get("tools") or []:
            expose_mode = t.get("expose", True)
            if expose_mode is True:
                self.tools[t["name"]] = t
            elif expose_mode == "operator":
                self.operator_tools[t["name"]] = t
        for c in self.cfg.get("composites") or []:
            if c.get("expose", True):
                self.composites[c["name"]] = c
        self._token_cache = {}

    # ---- base url / safety
    @property
    def base_url(self):
        raw = os.environ.get("APITOOL_BASE_URL") or (self.cfg.get("api") or {}).get("base_url") or ""
        if not raw:
            servers = self.spec.get("servers") or []
            raw = servers[0]["url"] if servers else ""
        def _sub(m):
            var, def_val = m.group(1), m.group(2)
            val = os.environ.get(var)
            if val is not None:
                return val
            if def_val is not None:
                return def_val
            return ""
        url = re.sub(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}", _sub, raw)
        return url.rstrip("/")


    def _validate_base_url(self):
        url = self.base_url
        if not url:
            return
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        is_loopback = host in ("localhost", "127.0.0.1", "::1")
        allow_external = bool((self.cfg.get("security") or {}).get("allow_external_urls", False)) or (os.environ.get("APITOOL_ALLOW_EXTERNAL") == "1")
        if not is_loopback and not allow_external:
            raise PermissionError(f"Security policy violation: Non-loopback base URL host '{host}' is forbidden unless explicitly enabled in config (security.allow_external_urls: true) and approved at Gate G4.")

    def _is_local(self):
        host = urllib.parse.urlparse(self.base_url).hostname
        return host in ("localhost", "127.0.0.1", "::1")

    def _writes_allowed(self):
        return (os.environ.get("APITOOL_ALLOW_WRITES") == "1" or self._is_local()) and os.environ.get("APITOOL_DISABLE_WRITES") != "1"

    # ---- schema building
    def _merged_params(self, opinfo):
        merged = {}
        for p in list(opinfo["common_params"]) + list(opinfo["op"].get("parameters", [])):
            p = deref(self.spec, p)
            merged[(p.get("in"), p.get("name"))] = p
        return list(merged.values())

    def _plan(self, opinfo, tool_cfg):
        """Return (json_schema, plan). plan tells us how to assemble the HTTP request."""
        icfg = (tool_cfg or {}).get("inputs") or {}
        fixed = {**(self.defaults.get("fixed") or {}), **(icfg.get("fixed") or {})}
        defaults_in = icfg.get("defaults") or {}
        include = icfg.get("include")
        rename = icfg.get("rename") or {}
        props, required, args = {}, [], {}
        fixed_list = []

        def add(loc, real, schema, is_req, desc):
            if real in fixed:
                fixed_list.append((loc, real, fixed[real]))
                return
            name = rename.get(real, real)
            if name in props:
                name = f"{loc}_{name}"
            s = dict(schema or {"type": "string"})
            s.pop("readOnly", None)
            s.pop("xml", None)
            s.pop("example", None)
            if desc and not s.get("description"):
                s["description"] = desc
            if real in defaults_in:
                s["default"] = defaults_in[real]
                is_req = False
            props[name] = s
            args[name] = (loc, real)
            if is_req:
                required.append(name)

        for p in self._merged_params(opinfo):
            loc = p.get("in")
            if loc == "cookie":
                continue
            if loc == "header" and p["name"].lower() in ("authorization", "content-type", "accept", "user-agent"):
                continue
            sch = p.get("schema") or {"type": "string"}
            add(loc, p["name"], sch, bool(p.get("required")) or loc == "path", p.get("description", ""))

        body_mode, body_ct = "none", None
        rb = deref(self.spec, opinfo["op"].get("requestBody") or {})
        content = rb.get("content") or {}
        for ct in ("application/json", "application/x-www-form-urlencoded", "multipart/form-data"):
            match = next((c for c in content if c.split(";")[0].strip() == ct), None)
            if not match and ct == "application/json":
                match = next((c for c in content if "json" in c), None)
            if match:
                body_ct = match
                sch = content[match].get("schema") or {}
                if ct == "multipart/form-data":
                    body_mode = "unsupported"
                elif sch.get("type") == "object" or sch.get("properties"):
                    body_mode = "form" if "form-urlencoded" in ct else "json"
                    req_set = set(sch.get("required") or [])
                    for k, v in (sch.get("properties") or {}).items():
                        if v.get("readOnly"):
                            continue
                        add("body", k, v, k in req_set and rb.get("required", True) is not False, v.get("description", ""))
                else:
                    body_mode = "raw"
                    add("body", "body", sch, bool(rb.get("required")), rb.get("description", ""))
                break

        if include is not None:
            keep = set(include)
            for n in list(props):
                if n not in keep and n not in required:
                    props.pop(n)
                    args.pop(n)

        safety = (tool_cfg or {}).get("safety", "read")
        if safety != "read":
            props["dry_run"] = {"type": "boolean", "description": "If true, show the request that would be sent without sending it."}
        if safety == "destructive" or (tool_cfg or {}).get("confirm"):
            props["confirm"] = {"type": "boolean", "description": "Must be true to proceed. Ask the user before setting this."}
            required.append("confirm")
        pag = (tool_cfg or {}).get("output", {}).get("paginate")
        if pag:
            props["all_pages"] = {"type": "boolean", "description": f"Fetch every page (max {pag.get('max_pages', 5)})."}

        schema = {"type": "object", "properties": props, "required": sorted(set(required)), "additionalProperties": False}
        plan = {"args": args, "fixed": fixed_list, "body_mode": body_mode, "body_ct": body_ct}
        return schema, plan

    def list_tools(self):
        out = []
        for name, t in self.tools.items():
            if t.get("x-environment") == "mock" and not self._is_local():
                continue
            opinfo = self.ops.get(t["operation"])
            if not opinfo:
                continue
            schema, _ = self._plan(opinfo, t)
            out.append(self._tool_entry(name, t, schema))
        for name, c in self.composites.items():
            props = {k: dict(v) for k, v in (c.get("inputs") or {}).items()}
            req = [k for k, v in props.items() if v.pop("required", False)]
            if c.get("safety", "read") != "read":
                props["dry_run"] = {"type": "boolean", "description": "Preview only; do not send."}
            if c.get("safety") == "destructive" or c.get("confirm"):
                props["confirm"] = {"type": "boolean", "description": "Must be true to proceed."}
                req.append("confirm")
            out.append(self._tool_entry(name, c, {"type": "object", "properties": props, "required": req, "additionalProperties": False}))
        return out

    def list_operator_tools(self):
        out = []
        for name, t in self.operator_tools.items():
            opinfo = self.ops.get(t["operation"])
            if not opinfo:
                continue
            schema, _ = self._plan(opinfo, t)
            out.append(self._tool_entry(name, t, schema))
        return out

    def _tool_entry(self, name, t, schema):
        safety = t.get("safety", "read")
        return {"name": name, "description": t.get("description", name).strip(), "inputSchema": schema,
                "safety": safety, "readOnly": safety == "read", "destructive": safety == "destructive"}

    # ---- calling
    def call_tool(self, name, args=None):
        args = dict(args or {})
        try:
            if name in self.operator_tools:
                return self._final(error_result("not_found", f"Tool '{name}' not found."))
            elif name in self.composites:
                res = self._call_composite(self.composites[name], args)
            elif name in self.tools:
                t = self.tools[name]
                if t.get("x-environment") == "mock" and not self._is_local():
                    return self._final(error_result("safety_blocked", f"Tool '{name}' is marked x-environment: mock and cannot be called against live targets."))
                opinfo = self.ops.get(t["operation"])
                if not opinfo:
                    return self._final(error_result("bad_request", f"Tool '{name}' is bound to unknown operation '{t['operation']}'. Spec drift? Run drift_check."))
                res = self._call_op(opinfo, t, args)
            else:
                res = error_result("not_found", f"Unknown tool '{name}'. Available: {', '.join(sorted(list(self.tools) + list(self.composites)))}")
        except ValueError as e:
            res = error_result("validation", str(e))
        except KeyError as e:
            res = error_result("auth", f"Missing environment variable {e}. Set it (never paste secrets into chat).")
        except Exception as e:  # never leak a stack trace into the agent's context
            log("internal error:", repr(e))
            res = error_result("server", f"Internal tool error: {type(e).__name__}: {e}")
        return self._final(res)

    def call_operator_tool(self, name, args=None):
        args = dict(args or {})
        if name not in self.operator_tools:
            return self._final(error_result("not_found", f"Operator tool '{name}' not found. Available operator tools: {', '.join(sorted(self.operator_tools))}"))
        t = self.operator_tools[name]
        opinfo = self.ops.get(t["operation"])
        if not opinfo:
            return self._final(error_result("bad_request", f"Operator tool '{name}' bound to unknown operation '{t['operation']}'"))
        return self._final(self._call_op(opinfo, t, args))

    def set_approval_verifier(self, verifier_fn):
        self.approval_verifier = verifier_fn

    def execute_approved(self, action_id, tool_name, args, verifier=None):
        args = dict(args or {})
        fn = verifier or getattr(self, "approval_verifier", None)
        if not fn:
            return self._final(error_result("permission", "No approval verifier injected by host application. Approval tools always stage and never run."))
        tool_cfg = self.tools.get(tool_name) or self.operator_tools.get(tool_name)
        if not tool_cfg:
            return self._final(error_result("not_found", f"Tool '{tool_name}' not found."))
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        args_bytes = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
        args_sha = hashlib.sha256(args_bytes).hexdigest()
        ok, reason = fn(action_id, tool_name, args_sha)
        if not ok:
            log(f"[{now_iso}] [APPROVAL_REFUSED] action_id={action_id} reason={reason}")
            return self._final(error_result("permission", f"Approval verification failed: {reason}"))
        log(f"[{now_iso}] [APPROVAL_EXECUTED] action_id={action_id} tool={tool_name}")
        opinfo = self.ops.get(tool_cfg["operation"])
        if not opinfo:
            return self._final(error_result("bad_request", f"Tool '{tool_name}' bound to unknown operation '{tool_cfg['operation']}'"))
        return self._final(self._call_op(opinfo, tool_cfg, args, bypass_approval=True))

    def _final(self, res):
        limit = int(self.defaults.get("max_response_chars", 20000))
        text = json.dumps(res, default=str)
        if len(text) > limit and res.get("ok"):
            res = {"ok": True, "status": res.get("status"), "truncated": True,
                   "note": f"Response was {len(text)} chars; showing the first {limit}. Narrow the query or use output.pick.",
                   "data_preview": text[:limit]}
        return json.loads(redact(json.dumps(res, default=str)))

    def _call_op(self, opinfo, tool_cfg, args, bypass_approval=False):
        schema, plan = self._plan(opinfo, tool_cfg)
        safety = tool_cfg.get("safety", "read")
        want_all = bool(args.pop("all_pages", False))
        dry = bool(args.pop("dry_run", False))
        confirm = args.pop("confirm", None)

        # Reject any argument starting with '__' (reserved for internal use, cannot be injected by LLM)
        for k in args:
            if k.startswith("__"):
                return error_result("validation", f"Argument '{k}' is forbidden (arguments starting with '__' are reserved).")

        problems = self._validate_args(schema, args)
        if problems:
            return error_result("validation", "; ".join(problems))

        # Check approval: ui requirement - LLM/call_tool path NEVER executes approval: ui tools
        if not bypass_approval and tool_cfg.get("approval") == "ui":
            act_id = f"act_{uuid.uuid4().hex[:12]}"
            args_bytes = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
            args_hash = hashlib.sha256(args_bytes).hexdigest()
            redacted_args = json.loads(redact(json.dumps(args, default=str)))
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            log(f"[{now_iso}] [PENDING_ACTION_STAGED] {act_id} tool={tool_cfg.get('name')}")
            return {
                "ok": True,
                "status": "pending_user_approval",
                "action_id": act_id,
                "tool": tool_cfg.get("name"),
                "args_sha256": args_hash,
                "pending_action": {
                    "id": act_id,
                    "tool": tool_cfg.get("name"),
                    "args": redacted_args,
                    "args_sha256": args_hash,
                    "session_id": os.environ.get("SESSION_ID", "default_session"),
                    "created_at": now_iso,
                    "status": "pending"
                },
                "message": f"Action '{tool_cfg.get('name')}' staged. A result of pending_user_approval means NOT sent or scheduled yet; tell the user to approve in the UI."
            }

        if not bypass_approval and (safety == "destructive" or tool_cfg.get("confirm")) and confirm is not True:
            return error_result("safety_blocked", "This action needs explicit user approval. Ask the user, then call again with confirm=true.")
        for n, v in schema["properties"].items():
            if n not in args and "default" in v:
                args[n] = v["default"]

        pag = (tool_cfg.get("output") or {}).get("paginate")
        if want_all and pag:
            return self._paginate(opinfo, tool_cfg, plan, args, pag)

        req = self._build_request(opinfo, plan, args)
        if safety != "read" and (dry or not self._writes_allowed()):
            why = "dry_run requested" if dry else "writes are disabled (set APITOOL_ALLOW_WRITES=1 or target a localhost base URL)"
            return {"ok": True, "dry_run": True, "note": f"Not sent: {why}.", "would_send": self._preview(req)}
        return self._send(req, tool_cfg, opinfo)

    @staticmethod
    def _validate_args(schema, args):
        problems = []
        for k in args:
            if k.startswith("__"):
                problems.append(f"argument '{k}' is forbidden (reserved internal prefix '__')")
        props = schema["properties"]
        unknown = [k for k in args if k not in props and not k.startswith("__")]
        if unknown:
            problems.append(f"unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(props))}")
        for r in schema["required"]:
            if r not in args and r != "confirm" and "default" not in props.get(r, {}):
                problems.append(f"missing required argument '{r}'")
        for k, v in args.items():
            s = props.get(k)
            if not s:
                continue
            t = s.get("type")
            if t == "integer" and isinstance(v, str) and re.fullmatch(r"-?\d+", v):
                args[k] = int(v)
                v = args[k]
            elif t == "number" and isinstance(v, str):
                try:
                    args[k] = float(v)
                    v = args[k]
                except ValueError:
                    pass
            elif t == "boolean" and isinstance(v, str) and v.lower() in ("true", "false"):
                args[k] = v.lower() == "true"
                v = args[k]
            if "enum" in s and v not in s["enum"]:
                problems.append(f"'{k}' must be one of {s['enum']}")
            if isinstance(v, str):
                if "pattern" in s and not re.fullmatch(s["pattern"], v):
                    problems.append(f"'{k}' does not match pattern {s['pattern']}")
                if "minLength" in s and len(v) < int(s["minLength"]):
                    problems.append(f"'{k}' length must be >= {s['minLength']}")
                if "maxLength" in s and len(v) > int(s["maxLength"]):
                    problems.append(f"'{k}' length must be <= {s['maxLength']}")
            if isinstance(v, (int, float)):
                if "minimum" in s and v < s["minimum"]:
                    problems.append(f"'{k}' must be >= {s['minimum']}")
                if "maximum" in s and v > s["maximum"]:
                    problems.append(f"'{k}' must be <= {s['maximum']}")
        return problems

    def _build_request(self, opinfo, plan, args):
        path, query, headers, body = opinfo["path"], {}, {}, {}
        items = [(loc, real, args[name]) for name, (loc, real) in plan["args"].items() if name in args]
        items += [(loc, real, expand_env(val)) for loc, real, val in plan["fixed"]]
        for loc, real, val in items:
            if loc == "path":
                path = path.replace("{" + real + "}", urllib.parse.quote(str(val), safe=""))
            elif loc == "query":
                query[real] = val
            elif loc == "header":
                headers[real] = str(val)
            elif loc == "body":
                body[real] = val
        data = None
        mode = plan["body_mode"]
        if mode == "json":
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif mode == "form":
            data = urllib.parse.urlencode({k: ("true" if v is True else "false" if v is False else v) for k, v in body.items()}, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif mode == "raw" and "body" in body:
            data = json.dumps(body["body"]).encode()
            headers["Content-Type"] = "application/json"
        if unresolved := re.findall(r"\{[^}]+\}", path):
            raise ValueError(f"unresolved path parameter {unresolved[0]}")
        url = self.base_url + path
        if query:
            qs = urllib.parse.urlencode({k: ("true" if v is True else "false" if v is False else v) for k, v in query.items()}, doseq=True)
            url += ("&" if "?" in url else "?") + qs
        if opinfo["method"] == "POST" and self.defaults.get("idempotency_header"):
            headers[self.defaults["idempotency_header"]] = str(uuid.uuid4())
        return {"method": opinfo["method"], "url": url, "headers": headers, "data": data}

    def _preview(self, req):
        body = None
        if req["data"]:
            try:
                body = req["data"].decode()
            except Exception:
                body = "<binary>"
        return {"method": req["method"], "url": req["url"], "headers": {k: v for k, v in req["headers"].items()}, "body": body}

    # ---- auth
    def _apply_auth(self, req, tool_cfg=None, opinfo=None):
        tool_auth = (tool_cfg or {}).get("auth")
        if tool_auth == "none" or tool_auth is False:
            return
        a = tool_auth if isinstance(tool_auth, dict) else (self.cfg.get("auth") or {"type": "none"})
        t = a.get("type", "none")
        if t == "none":
            return
        if t == "basic":
            u, p = os.environ[a["username_env"]], os.environ[a["password_env"]]
            req["headers"]["Authorization"] = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
        elif t == "bearer":
            req["headers"]["Authorization"] = "Bearer " + os.environ[a["token_env"]]
        elif t == "apikey":
            val = os.environ[a["value_env"]]
            if a.get("in", "header") == "header":
                req["headers"][a["name"]] = val
            else:
                sep = "&" if "?" in req["url"] else "?"
                req["url"] += f"{sep}{urllib.parse.quote(a['name'])}={urllib.parse.quote(val)}"
        elif t == "oauth2_client_credentials":
            req["headers"]["Authorization"] = "Bearer " + self._oauth_token(a)
        else:
            raise ValueError(f"unsupported auth type '{t}' (see references/auth-patterns.md)")

    def _oauth_token(self, a):
        cached = self._token_cache.get(a["token_url"])
        if cached and cached["exp"] > time.time() + 30:
            return cached["token"]
        cid, sec = os.environ[a["client_id_env"]], os.environ[a["client_secret_env"]]
        form = {"grant_type": "client_credentials"}
        if a.get("scope"):
            form["scope"] = a["scope"]
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if a.get("auth_style", "basic") == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()
        else:
            form.update(client_id=cid, client_secret=sec)
        r = urllib.request.Request(a["token_url"], data=urllib.parse.urlencode(form).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(r, timeout=20) as resp:
            data = json.loads(resp.read())
        self._token_cache[a["token_url"]] = {"token": data["access_token"], "exp": time.time() + int(data.get("expires_in", 300))}
        return data["access_token"]

    # ---- http
    def _send(self, req, tool_cfg, opinfo):
        self._validate_base_url()
        self._apply_auth(req, tool_cfg, opinfo)
        req["headers"].setdefault("Accept", "application/json")
        req["headers"].setdefault("User-Agent", f"apitool/{VERSION}")
        timeout = float(self.defaults.get("timeout_s", 30))
        max_retries = int(self.defaults.get("max_retries", 3))
        idempotent = opinfo["method"] in ("GET", "HEAD", "PUT", "DELETE", "OPTIONS") or bool(self.defaults.get("idempotency_header"))
        attempt = 0
        while True:
            attempt += 1
            r = urllib.request.Request(req["url"], data=req["data"], headers=req["headers"], method=req["method"])
            try:
                with urllib.request.urlopen(r, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return self._success(resp.status, raw, resp.headers, tool_cfg)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                retry_ok = (e.code == 429) or (e.code in (502, 503, 504) and idempotent)
                if retry_ok and attempt <= max_retries:
                    ra = e.headers.get("Retry-After")
                    delay = min(float(ra), 30) if ra and ra.replace(".", "", 1).isdigit() else min(2 ** (attempt - 1) + random.random(), 30)
                    log(f"retry {attempt}/{max_retries} after HTTP {e.code} in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                return normalize_http_error(e.code, raw, e.headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if idempotent and attempt <= max_retries:
                    time.sleep(min(2 ** (attempt - 1) + random.random(), 30))
                    continue
                kind = "timeout" if isinstance(e, TimeoutError) or "timed out" in str(e) else "network"
                return error_result(kind, f"{type(e).__name__}: {getattr(e, 'reason', e)}")

    def _success(self, status, raw, headers, tool_cfg):
        data = raw
        try:
            data = json.loads(raw) if raw.strip() else None
        except Exception:
            pass
        data, truncated = self._project(data, tool_cfg.get("output") or {})
        res = {"ok": True, "status": status, "data": data}
        if truncated:
            res["truncated"] = True
        rl = {k: v for k, v in headers.items() if "ratelimit" in k.lower()}
        if rl:
            res["rate_limit"] = rl
        return res

    @staticmethod
    def _project(data, out):
        truncated = False
        items_path = out.get("items_path")
        container, items = data, data
        if items_path and isinstance(data, (dict, list)):
            items = dotted_get(data, items_path, data)
        pick = out.get("pick")
        mask = out.get("mask") or {}

        def mask_val(field_name, val):
            rule = mask.get(field_name)
            if rule == "last4" and isinstance(val, str):
                prefix = "+" if val.startswith("+") else ""
                raw = val[1:] if val.startswith("+") else val
                if len(raw) > 4:
                    return prefix + ("*" * (len(raw) - 4)) + raw[-4:]
            return val

        def pick_one(obj):
            if not isinstance(obj, dict):
                return obj
            res = {}
            target_keys = pick if pick else list(obj.keys())
            for p in target_keys:
                v = dotted_get(obj, p, None)
                if v is not None:
                    v = mask_val(p, v)
                    dotted_set(res, p, v)
            return res
        if isinstance(items, list):
            mx = out.get("max_items")
            if mx and len(items) > mx:
                items, truncated = items[:mx], True
            items = [pick_one(i) for i in items]
            if items_path and isinstance(container, dict):
                container = dict(container)
                dotted_set(container, items_path, items)
                return (container if not pick else {items_path: items}), truncated
            return items, truncated
        if items_path and isinstance(items, dict):
            return pick_one(items), truncated
        return pick_one(data), truncated

    def _paginate(self, opinfo, tool_cfg, plan, args, pag):
        style = pag.get("style", "cursor")
        max_pages = int(pag.get("max_pages", 5))
        out_cfg = tool_cfg.get("output") or {}
        items_path = out_cfg.get("items_path")
        collected, last, args = [], None, dict(args)
        next_url = None
        for page in range(max_pages):
            req = self._build_request(opinfo, plan, args)
            if next_url:
                req["url"] = next_url if next_url.startswith("http") else self.base_url + next_url
            res = self._send(req, {"output": {}}, opinfo)
            if not res["ok"]:
                return res
            body = res["data"]
            chunk = dotted_get(body, items_path, []) if items_path else body
            chunk = chunk if isinstance(chunk, list) else []
            collected += chunk
            last = res
            if style == "cursor":
                nxt = dotted_get(body, pag["next_path"]) if isinstance(body, dict) else None
                if not nxt:
                    break
                args[pag["param"]] = nxt
            elif style == "page":
                if not chunk:
                    break
                args[pag["param"]] = int(args.get(pag["param"], pag.get("start", 1))) + 1
            elif style == "offset":
                if not chunk:
                    break
                args[pag["param"]] = int(args.get(pag["param"], 0)) + len(chunk)
            elif style == "next_url":
                next_url = dotted_get(body, pag["next_path"]) if isinstance(body, dict) else None
                if not next_url:
                    break
        mx = out_cfg.get("max_items")
        pick = out_cfg.get("pick")
        trunc = False
        if mx and len(collected) > mx:
            collected, trunc = collected[:mx], True
        if pick:
            collected = [self._project(c, {"pick": pick})[0] for c in collected]
        res = {"ok": True, "status": last["status"], "data": collected, "pages_fetched": page + 1}
        if trunc:
            res["truncated"] = True
        return res

    # ---- composites (multi-call tools designed around user intent)
    def _call_composite(self, comp, args):
        safety = comp.get("safety", "read")
        dry = bool(args.pop("dry_run", False))
        confirm = args.pop("confirm", None)
        props = comp.get("inputs") or {}
        missing = [k for k, v in props.items() if v.get("required") and k not in args]
        unknown = [k for k in args if k not in props]
        if missing or unknown:
            return error_result("validation", f"missing: {missing}; unknown: {unknown}")
        if (safety == "destructive" or comp.get("confirm")) and confirm is not True:
            return error_result("safety_blocked", "Needs explicit user approval. Ask the user, then call again with confirm=true.")
        ctx = {"input": args, "steps": {}}
        if safety != "read" and (dry or not self._writes_allowed()):
            plan = []
            for st in comp["steps"]:
                opinfo = self.ops.get(st["call"])
                if opinfo is None:
                    return error_result("bad_request", f"composite step '{st['id']}' calls unknown operation '{st['call']}'")
                plan.append({"step": st["id"], "operation": st["call"], "method": opinfo["method"], "path": opinfo["path"], "args": render(st.get("args", {}), ctx)})
            return {"ok": True, "dry_run": True, "note": "Not sent (writes disabled or dry_run).", "planned_steps": plan}
        for st in comp["steps"]:
            opinfo = self.ops.get(st["call"])
            if opinfo is None:
                return error_result("bad_request", f"composite step '{st['id']}' calls unknown operation '{st['call']}'")
            step_args = render(st.get("args", {}), ctx)
            step_cfg = {"safety": "read" if opinfo["method"] == "GET" else "write", "inputs": {"fixed": st.get("fixed", {}) or (comp.get("fixed") or {})},
                        "output": st.get("output") or {}}
            schema, plan = self._plan(opinfo, step_cfg)
            req = self._build_request(opinfo, plan, {k: v for k, v in step_args.items() if v is not None})
            res = self._send(req, step_cfg, opinfo)
            if not res["ok"]:
                res["error"]["failed_step"] = st["id"]
                return res
            ctx["steps"][st["id"]] = {"data": res["data"], "status": res["status"]}
        out = render(comp.get("output") or {"steps": ctx["steps"]}, ctx)
        return {"ok": True, "data": out}


def main():  # tiny self-check: python core.py  -> lists tools
    t = ApiTools(Path(__file__).parent)
    for e in t.list_tools():
        print(f"{e['name']:<32} [{e['safety']}] {e['description'][:70]}")


if __name__ == "__main__":
    main()
