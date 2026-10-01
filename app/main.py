from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import sessions
from .analyzer import analyze, analyze_guarantee, plan_completion, plan_deactivation
from .schemas import (
    CompletionRequest,
    CompletionResponse,
    CreatedSessionView,
    DeadlockRequest,
    DeadlockResponse,
    DeactivationRequest,
    DeactivationResponse,
    GuaranteeRequest,
    GuaranteeResponse,
    ReplaceProtocolRequest,
    SessionError,
    SessionView,
    VersionedActionRequest,
)

app = FastAPI(
    title="Offline Two-Party Message Protocol Deadlock Analyzer",
    version="1.4.0",
)


@app.exception_handler(SessionError)
def session_error_handler(request: Request, exc: SessionError) -> JSONResponse:
    detail: dict = {"code": exc.code, "message": exc.message}
    detail.update(exc.details)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deadlocks", response_model=DeadlockResponse)
def detect_deadlocks(request: DeadlockRequest) -> dict:
    return analyze(request)


@app.post("/v1/completion-plans", response_model=CompletionResponse)
def plan_completion_endpoint(request: CompletionRequest) -> dict:
    return plan_completion(request)


@app.post("/v1/deactivation-plans", response_model=DeactivationResponse)
def plan_deactivation_endpoint(request: DeactivationRequest) -> dict:
    return plan_deactivation(request)


@app.post("/v1/completion-guarantees", response_model=GuaranteeResponse)
def analyze_completion_guarantee(request: GuaranteeRequest) -> dict:
    return analyze_guarantee(request)


# ---------------------------------------------------------------------------
# Stateful two-endpoint protocol drill sessions
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", response_model=CreatedSessionView, status_code=201)
def create_session(request: DeadlockRequest) -> SessionView:
    _state, view = sessions.create_session(request)
    return view


@app.get("/v1/sessions/{session_id}", response_model=SessionView)
def get_session(session_id: str) -> SessionView:
    _state, view = sessions.get_session(session_id)
    return view


@app.post(
    "/v1/sessions/{session_id}/transitions/{transition_id}",
    response_model=SessionView,
)
def execute_session_transition(
    session_id: str, transition_id: str, request: VersionedActionRequest
) -> SessionView:
    _state, view = sessions.execute_transition(
        session_id, transition_id, request.expected_version
    )
    return view


@app.put("/v1/sessions/{session_id}/protocol", response_model=SessionView)
def replace_session_protocol(session_id: str, request: ReplaceProtocolRequest) -> SessionView:
    _state, view = sessions.replace_protocol(
        session_id, request.protocol, request.expected_version
    )
    return view
