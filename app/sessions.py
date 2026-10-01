"""In-process, single-process drill sessions.

A session keeps the current protocol, the ordered list of already executed
transition ids (the trace), the version counter and the derived current
configuration. All state lives in module-level memory: restarting the process
empties every session.

Mutations use optimistic concurrency: the caller supplies the version it saw;
only a mutation carrying the current version succeeds, after which the version
is incremented. A single global lock makes check-and-mutate atomic, so two
concurrent mutations against the same version cannot both succeed, and the
protocol, configuration, trace and version always take effect together.
"""

import threading
import uuid

from .analyzer import (
    Configuration,
    apply_transition,
    build_model,
    enabled_transitions,
    is_completion,
)
from .schemas import DeadlockRequest, SessionError, SessionView


class SessionState:
    def __init__(self, session_id: str, protocol: DeadlockRequest):
        self.session_id = session_id
        self.version = 0
        self.protocol = protocol
        # (destination configuration, transition) pairs, in execution order.
        self.trace: list[tuple[Configuration, object]] = []
        self.model = build_model(protocol)
        self.current: Configuration = self.model.initial


_SESSIONS: dict[str, SessionState] = {}
_LOCK = threading.Lock()


def create_session(protocol: DeadlockRequest) -> tuple[SessionState, SessionView]:
    with _LOCK:
        session_id = uuid.uuid4().hex
        while session_id in _SESSIONS:
            session_id = uuid.uuid4().hex
        session = SessionState(session_id, protocol)
        _SESSIONS[session_id] = session
        return session, _snapshot(session)


def get_session(session_id: str) -> tuple[SessionState, SessionView]:
    with _LOCK:
        session = _require(session_id)
        return session, _snapshot(session)


def execute_transition(
    session_id: str, transition_id: str, expected_version: int
) -> tuple[SessionState, SessionView]:
    with _LOCK:
        session = _require(session_id)
        _check_version(session, expected_version)

        if is_completion(session.current, session.model.finals):
            raise SessionError(
                409,
                "session_completed",
                "the session has already reached a completion configuration",
            )

        fireable = {
            transition.id: transition
            for transition in enabled_transitions(session.current, session.model.outgoing)
        }
        transition = fireable.get(transition_id)
        if transition is None:
            raise SessionError(
                409,
                "transition_not_enabled",
                f"transition {transition_id!r} is not enabled in the current configuration",
                {
                    "transition_id": transition_id,
                    "enabled_transition_ids": sorted(fireable),
                },
            )

        nxt = apply_transition(session.current, transition)
        # Protocol, configuration, trace and version commit together.
        session.trace.append((nxt, transition))
        session.current = nxt
        session.version += 1
        return session, _snapshot(session)


def replace_protocol(
    session_id: str, protocol: DeadlockRequest, expected_version: int
) -> tuple[SessionState, SessionView]:
    with _LOCK:
        session = _require(session_id)
        _check_version(session, expected_version)

        model = build_model(protocol)
        by_id = {transition.id: transition for transition in model.transitions}

        # Replay every executed id from the new initial configuration. The
        # transition definitions may change, but each historical step must
        # remain fireable, and the trace must not reach completion before it
        # ends (completion configurations never expand).
        configuration = model.initial
        replayed: list[tuple[Configuration, object]] = []
        for step_number, (_old_configuration, old_transition) in enumerate(session.trace, start=1):
            if is_completion(configuration, model.finals):
                raise _replay_failure(step_number, old_transition.id, session)
            transition = by_id.get(old_transition.id)
            if transition is None:
                raise _replay_failure(step_number, old_transition.id, session)
            fireable = {
                item.id: item
                for item in enabled_transitions(configuration, model.outgoing)
            }
            if transition.id not in fireable:
                raise _replay_failure(step_number, transition.id, session)
            configuration = apply_transition(configuration, transition)
            replayed.append((configuration, transition))

        # All checks passed: commit the new protocol, derived configuration,
        # trace and version as a single atomic state change.
        session.protocol = protocol
        session.model = model
        session.trace = replayed
        session.current = configuration
        session.version += 1
        return session, _snapshot(session)


def _replay_failure(step_number: int, transition_id: str, session: SessionState) -> SessionError:
    return SessionError(
        422,
        "replay_failed",
        (
            f"the executed trace cannot be replayed under the replacement protocol: "
            f"step {step_number} (transition {transition_id!r}) is not fireable "
            f"before the end of the trace"
        ),
        {"failed_step": step_number, "transition_id": transition_id},
    )


def _require(session_id: str) -> SessionState:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise SessionError(404, "session_not_found", f"unknown session id: {session_id}")
    return session


def _check_version(session: SessionState, expected_version: int) -> None:
    if expected_version != session.version:
        raise SessionError(
            409,
            "version_conflict",
            f"expected version {session.version}, got {expected_version}",
            {"current_version": session.version, "expected_version": expected_version},
        )


def _snapshot(session: SessionState) -> SessionView:
    configuration = session.current
    enabled_ids = [
        transition.id
        for transition in enabled_transitions(configuration, session.model.outgoing)
    ]
    steps = [
        {
            "index": index,
            "transition_id": transition.id,
            "from": transition.sender,
            "to": transition.receiver,
            "action": transition.action,
            "message": transition.message,
            "state": {"A": config.state_a, "B": config.state_b},
            "queues": {
                "A_to_B": config.queue_a_to_b,
                "B_to_A": config.queue_b_to_a,
            },
        }
        for index, (config, transition) in enumerate(session.trace, start=1)
    ]
    return SessionView(
        session_id=session.session_id,
        version=session.version,
        completed=is_completion(configuration, session.model.finals),
        protocol=session.protocol,
        configuration={
            "state": {"A": configuration.state_a, "B": configuration.state_b},
            "queues": {
                "A_to_B": configuration.queue_a_to_b,
                "B_to_A": configuration.queue_b_to_a,
            },
        },
        trace=steps,
        enabled_transition_ids=enabled_ids,
    )
