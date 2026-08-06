"""Data access. Azure Table Storage when configured, in-memory otherwise.

requests: PartitionKey = location, RowKey = request_id
sessions: PartitionKey = device_id, RowKey = session_id  (transient, TTL'd)

The in-memory backend is not a test double -- it is what runs before the
deployment lead has provisioned storage, and it keeps `func start` working on a
laptop with no Azure account.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import config

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# In-memory backend
# --------------------------------------------------------------------------
class MemoryStore:
    name = "in-memory"

    def __init__(self):
        self._requests = {}
        self._sessions = {}
        self._seen = set()
        self._lock = threading.Lock()

    # --- requests
    def put_request(self, rec):
        with self._lock:
            self._requests[rec["request_id"]] = dict(rec)
        return rec

    def get_request(self, request_id):
        with self._lock:
            rec = self._requests.get(request_id)
            return dict(rec) if rec else None

    def list_requests(self, status=None, location=None, since=None, limit=200):
        with self._lock:
            items = [dict(r) for r in self._requests.values()]
        if status:
            items = [r for r in items if r["status"] == status]
        if location:
            items = [r for r in items if r["location"] == location]
        if since:
            items = [r for r in items if r["created_at"] >= since]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    def update_request(self, request_id, changes):
        with self._lock:
            rec = self._requests.get(request_id)
            if not rec:
                return None
            rec.update(changes)
            return dict(rec)

    def next_sequence(self):
        with self._lock:
            return len(self._requests) + 817

    # --- sessions
    def get_session(self, device_id, session_id):
        with self._lock:
            s = self._sessions.get(session_id)
        if not s:
            return None
        if parse_iso(s["expires_at"]) < now():
            self.delete_session(device_id, session_id)
            return None
        return dict(s)

    def put_session(self, s):
        s = dict(s)
        s["expires_at"] = iso(now() + timedelta(seconds=config.SESSION_TTL_SECONDS))
        with self._lock:
            self._sessions[s["session_id"]] = s
        return s

    def delete_session(self, device_id, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    # --- idempotency
    def seen_turn(self, session_id, turn, timestamp):
        key = "%s|%s|%s" % (session_id, turn, timestamp)
        with self._lock:
            if key in self._seen:
                return True
            self._seen.add(key)
            return False


# --------------------------------------------------------------------------
# Azure Table Storage backend
# --------------------------------------------------------------------------
class TableStore:
    name = "azure-table"

    def __init__(self):
        from azure.data.tables import TableServiceClient
        svc = TableServiceClient.from_connection_string(config.STORAGE_CONNECTION_STRING)
        for t in (config.TABLE_REQUESTS, config.TABLE_SESSIONS, "turnlog"):
            try:
                svc.create_table(t)
            except Exception:
                pass  # already exists
        self._requests = svc.get_table_client(config.TABLE_REQUESTS)
        self._sessions = svc.get_table_client(config.TABLE_SESSIONS)
        self._turns = svc.get_table_client("turnlog")

    @staticmethod
    def _to_entity(rec, pk, rk):
        e = {"PartitionKey": pk, "RowKey": rk}
        for k, v in rec.items():
            e[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        return e

    @staticmethod
    def _from_entity(e):
        rec = {k: v for k, v in e.items()
               if k not in ("PartitionKey", "RowKey", "Timestamp", "etag")}
        for k in ("turns", "missing_fields"):
            if isinstance(rec.get(k), str):
                try:
                    rec[k] = json.loads(rec[k])
                except ValueError:
                    pass
        return rec

    # --- requests
    def put_request(self, rec):
        self._requests.upsert_entity(self._to_entity(rec, rec["location"], rec["request_id"]))
        return rec

    def get_request(self, request_id):
        items = list(self._requests.query_entities(
            "RowKey eq @rid", parameters={"rid": request_id}))
        return self._from_entity(items[0]) if items else None

    def list_requests(self, status=None, location=None, since=None, limit=200):
        clauses, params = [], {}
        if location:
            clauses.append("PartitionKey eq @loc"); params["loc"] = location
        if status:
            clauses.append("status eq @st"); params["st"] = status
        if since:
            clauses.append("created_at ge @since"); params["since"] = since
        q = " and ".join(clauses) if clauses else None
        it = (self._requests.query_entities(q, parameters=params) if q
              else self._requests.list_entities())
        items = [self._from_entity(e) for e in it]
        items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return items[:limit]

    def update_request(self, request_id, changes):
        rec = self.get_request(request_id)
        if not rec:
            return None
        rec.update(changes)
        return self.put_request(rec)

    def next_sequence(self):
        # Table Storage has no counter; the id only needs to be unique and readable.
        return int(now().timestamp()) % 10000

    # --- sessions
    def get_session(self, device_id, session_id):
        try:
            e = self._sessions.get_entity(device_id, session_id)
        except Exception:
            return None
        s = self._from_entity(e)
        if parse_iso(s["expires_at"]) < now():
            self.delete_session(device_id, session_id)
            return None
        return s

    def put_session(self, s):
        s = dict(s)
        s["expires_at"] = iso(now() + timedelta(seconds=config.SESSION_TTL_SECONDS))
        self._sessions.upsert_entity(self._to_entity(s, s["device_id"], s["session_id"]))
        return s

    def delete_session(self, device_id, session_id):
        try:
            self._sessions.delete_entity(device_id, session_id)
        except Exception:
            pass

    # --- idempotency
    def seen_turn(self, session_id, turn, timestamp):
        rk = "%s-%s" % (turn, timestamp.replace(":", ""))
        try:
            self._turns.get_entity(session_id, rk)
            return True
        except Exception:
            self._turns.upsert_entity({"PartitionKey": session_id, "RowKey": rk,
                                       "seen_at": iso(now())})
            return False


_store = None


def get_store():
    global _store
    if _store is None:
        if config.USE_TABLE_STORAGE:
            try:
                _store = TableStore()
                log.info("store: azure table storage")
            except Exception as exc:
                log.error("table storage unavailable (%s) -- falling back to memory", exc)
                _store = MemoryStore()
        else:
            log.warning("AZURE_STORAGE_CONNECTION_STRING not set -- using in-memory store")
            _store = MemoryStore()
    return _store
