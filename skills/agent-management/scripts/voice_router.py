"""Resolve spoken intent to verified Buddy IDs and retain a durable conversation target."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


class RoutingError(Exception):
    pass


def one(items, label):
    if len(items) != 1:
        raise RoutingError(f"Choose one {label}; found {len(items)} matching candidates. Use list to inspect IDs.")
    return items[0]


def choose(params, snapshot, saved):
    projects = snapshot.get("projects", [])
    sessions = [s for s in snapshot.get("sessions", []) if not s.get("closed")]
    active = snapshot.get("activeContext")
    explicit = any(params.get(k) for k in ("project_id", "session_id", "project", "worktree_path", "worktree"))
    target = params.get("target", "explicit" if explicit else "previous")
    if target not in ("current", "active", "previous", "explicit"):
        raise RoutingError("target must be current, active, previous or explicit")
    if target != "explicit" and explicit:
        raise RoutingError("Do not mix a context target with explicit selectors")
    context = active if target in ("active", "current") else (saved or active) if target == "previous" else None
    if target in ("active", "current", "previous") and not context:
        raise RoutingError("No selected desktop context or retained voice session. Choose a project/session.")
    pid = context.get("projectId") if context else params.get("project_id")
    project_name = params.get("project")
    project = one([p for p in projects if (not pid or p["id"] == pid) and
                   (not project_name or p["id"] == project_name or p["name"] == project_name)], "project")
    trees = snapshot.get("projectWorktrees", {}).get(project["id"], [])
    sid = context.get("sessionId") if context else params.get("session_id")
    if sid and not params.get("new_session"):
        session = one([s for s in sessions if s["id"] == sid and s["projectId"] == project["id"]], "session")
        if context and session["worktreePath"] != context.get("worktreePath"):
            raise RoutingError("Retained session no longer belongs to the selected worktree")
        if params.get("worktree_path") and session["worktreePath"] != params["worktree_path"]:
            raise RoutingError("Session does not match the requested worktree")
        tree = one([t for t in trees if t["path"] == session["worktreePath"] and
                    (not params.get("worktree") or params["worktree"] in (t["path"], t.get("branch")))], "worktree")
    else:
        tree_path = context.get("worktreePath") if context else params.get("worktree_path")
        tree_name = params.get("worktree")
        tree = one([t for t in trees if (not tree_path or t["path"] == tree_path) and
                    (not tree_name or tree_name in (t["path"], t.get("branch")))], "worktree")
        session = None
        if not params.get("new_session"):
            # A selected shell or empty selected workspace is not permission to
            # pick an arbitrary coding session in a different tab.
            if context:
                raise RoutingError("Selected worktree has no selected agent session. Select one or explicitly create a new session.")
            session = one([s for s in sessions if s["projectId"] == project["id"] and
                           s["worktreePath"] == tree["path"] and s["provider"] != "terminal" and
                           (not params.get("provider") or s["provider"] == params["provider"])], "agent session")
    provider = params.get("provider") or (session or {}).get("provider")
    if provider not in ("codex", "claude"):
        raise RoutingError("Choose Codex or Claude; shell terminals cannot receive voice agent prompts")
    if session and provider != session["provider"]:
        raise RoutingError("Provider does not match selected session; explicitly select or create the intended session")
    if params.get("new_session") and not any(p.get("id") == provider and p.get("available") for p in snapshot.get("providers", [])):
        raise RoutingError("Requested provider is unavailable on the paired desktop")
    return {"projectId": project["id"], "worktreePath": tree["path"],
            **({"sessionId": session["id"]} if session else {})}, provider


class VoiceRouter:
    def __init__(self, command, state_path=None):
        self.command = command
        self.path = Path(state_path or os.environ.get("BUDDY_VOICE_STATE", str(Path.home() / ".local/state/autonomous/buddy-voice.json")))

    def save(self, state):
        fd, name = tempfile.mkstemp(prefix=".buddy-voice-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def run(self, params):
        if not isinstance(params, dict):
            raise RoutingError("voice params must be an object")
        operation = params.get("operation", "send")
        if operation not in ("send", "status", "stop", "select", "resolve"):
            raise RoutingError("operation must be send, status, stop, select or resolve")
        if "new_session" in params and type(params["new_session"]) is not bool:
            raise RoutingError("new_session must be boolean")
        if params.get("new_session") and operation != "send":
            raise RoutingError("new_session is supported only with send")
        conversation = params.get("conversation_id", "voice")
        if not isinstance(conversation, str) or not 1 <= len(conversation) <= 128:
            raise RoutingError("conversation_id must contain 1–128 characters")
        if operation == "send":
            for key in ("request_id", "prompt"):
                if not isinstance(params.get(key), str) or not params[key].strip():
                    raise RoutingError(f"{key} is required for send")
            if len(params["request_id"]) > 128 or len(params["prompt"]) > 100000:
                raise RoutingError("request_id or prompt exceeds its limit")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {"version": 1, "conversations": {}}
                if state.get("version") != 1 or not isinstance(state.get("conversations"), dict):
                    raise ValueError("invalid state")
            except (ValueError, OSError, AttributeError) as exc:
                raise RoutingError("Voice context is unreadable; preserved for inspection, no command sent") from exc
            contexts = state["conversations"]
            if conversation not in contexts and len(contexts) >= 128:
                raise RoutingError("Voice context capacity reached; inspect stored conversations")
            context = contexts.setdefault(conversation, {"requests": {}})
            requests = context.setdefault("requests", {})
            request_id = params.get("request_id")
            fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prior = requests.get(request_id) if operation == "send" else None
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise RoutingError("request_id was already used for another voice request")
                if prior.get("result"):
                    return prior["result"]
                raise RoutingError("Previous delivery is uncertain. Inspect the recorded session; do not send again with a new ID.")
            if operation == "send" and any(not r.get("result") for r in requests.values()):
                raise RoutingError("An earlier voice request has uncertain delivery. Inspect status before any new send.")
            if operation == "resolve":
                # A human can abandon an uncertain request after reviewing the
                # terminal; never infer non-delivery from missing output alone.
                if params.get("resolution") != "do_not_retry" or params.get("request_id") not in requests:
                    raise RoutingError("Resolve requires an existing request_id and resolution do_not_retry after user review")
                if requests[params["request_id"]].get("result"):
                    raise RoutingError("Only an uncertain request can be resolved; completed receipts stay unchanged")
                requests[params["request_id"]]["result"] = {"abandoned": True, "request_id": params["request_id"]}
                self.save(state)
                return requests[params["request_id"]]["result"]
            snapshot = self.command("list", {})
            if not isinstance(snapshot, dict) or "projectWorktrees" not in snapshot:
                raise RoutingError("Buddy does not expose voice workspace context; update/open the desktop app")
            target, provider = choose(params, snapshot, context.get("target"))
            if operation == "select":
                context["target"] = target
                self.save(state)
                return {"selected": target, "provider": provider}
            if operation in ("status", "stop"):
                result = self.command("session" if operation == "status" else "stop", {
                    "project_id": target["projectId"], "session_id": target["sessionId"],
                    **({"after_seq": params.get("after_seq", 0)} if operation == "status" else {}),
                })
                return {"target": target, "detail": result, "uncertain_requests": [key for key, item in requests.items() if not item.get("result")]}
            # Persist the resolved target and uncertain receipt before any desktop
            # mutation, so a timeout/restart can never create a second session.
            token = hashlib.sha256((conversation + "\0" + request_id).encode()).hexdigest()
            if len(requests) >= 128:
                del requests[next(iter(requests))]
            receipt = {"fingerprint": fingerprint, "target": target}
            requests[request_id] = receipt
            self.save(state)
            def mutation(action, payload):
                try:
                    return self.command(action, payload)
                except Exception as exc:
                    if getattr(exc, "rejected", False):
                        receipt["result"] = {"accepted": False, "error": str(exc), "target": target}
                        self.save(state)
                    raise
            if params.get("new_session"):
                created = mutation("create", {"project_id": target["projectId"], "worktree_path": target["worktreePath"],
                    "provider": provider, "request_id": "voice-create-" + token, "title": params["prompt"][:70]})
                if not isinstance(created, dict) or not isinstance(created.get("id"), str) or not created["id"] or created.get("projectId") != target["projectId"] or created.get("worktreePath") != target["worktreePath"] or created.get("provider") != provider:
                    raise RoutingError("Create returned an unexpected session; delivery uncertain, inspect list before proceeding")
                target = {**target, "sessionId": created["id"]}
                receipt["target"] = target
                context["target"] = target
                self.save(state)
            context["target"] = target
            self.save(state)
            result = mutation("send", {"project_id": target["projectId"], "session_id": target["sessionId"],
                "request_id": "voice-send-" + token, "prompt": params["prompt"]})
            output = {"target": target, "provider": provider, "result": result}
            receipt["result"] = output
            self.save(state)
            return output
