import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RunMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_MODE_UNSPECIFIED: _ClassVar[RunMode]
    RUN_MODE_PERSONAL: _ClassVar[RunMode]
    RUN_MODE_DEMO: _ClassVar[RunMode]

class Stage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STAGE_UNSPECIFIED: _ClassVar[Stage]
    STAGE_FETCHING: _ClassVar[Stage]
    STAGE_PREFILTERING: _ClassVar[Stage]
    STAGE_INDEXING: _ClassVar[Stage]
    STAGE_MATCHING: _ClassVar[Stage]
    STAGE_SCORING: _ClassVar[Stage]
    STAGE_DRAFTING: _ClassVar[Stage]
    STAGE_AWAITING_APPROVAL: _ClassVar[Stage]
    STAGE_DONE: _ClassVar[Stage]
RUN_MODE_UNSPECIFIED: RunMode
RUN_MODE_PERSONAL: RunMode
RUN_MODE_DEMO: RunMode
STAGE_UNSPECIFIED: Stage
STAGE_FETCHING: Stage
STAGE_PREFILTERING: Stage
STAGE_INDEXING: Stage
STAGE_MATCHING: Stage
STAGE_SCORING: Stage
STAGE_DRAFTING: Stage
STAGE_AWAITING_APPROVAL: Stage
STAGE_DONE: Stage

class Source(_message.Message):
    __slots__ = ("kind", "slug", "name")
    KIND_FIELD_NUMBER: _ClassVar[int]
    SLUG_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    kind: str
    slug: str
    name: str
    def __init__(self, kind: _Optional[str] = ..., slug: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class Limits(_message.Message):
    __slots__ = ("fetch_concurrency", "match_top_k", "max_scored", "max_drafts", "min_draft_score")
    FETCH_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    MATCH_TOP_K_FIELD_NUMBER: _ClassVar[int]
    MAX_SCORED_FIELD_NUMBER: _ClassVar[int]
    MAX_DRAFTS_FIELD_NUMBER: _ClassVar[int]
    MIN_DRAFT_SCORE_FIELD_NUMBER: _ClassVar[int]
    fetch_concurrency: int
    match_top_k: int
    max_scored: int
    max_drafts: int
    min_draft_score: float
    def __init__(self, fetch_concurrency: _Optional[int] = ..., match_top_k: _Optional[int] = ..., max_scored: _Optional[int] = ..., max_drafts: _Optional[int] = ..., min_draft_score: _Optional[float] = ...) -> None: ...

class RadarRequest(_message.Message):
    __slots__ = ("profile_id", "mode", "sources", "limits")
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    mode: RunMode
    sources: _containers.RepeatedCompositeFieldContainer[Source]
    limits: Limits
    def __init__(self, profile_id: _Optional[str] = ..., mode: _Optional[_Union[RunMode, str]] = ..., sources: _Optional[_Iterable[_Union[Source, _Mapping]]] = ..., limits: _Optional[_Union[Limits, _Mapping]] = ...) -> None: ...

class RadarProgress(_message.Message):
    __slots__ = ("stage", "sources_total", "sources_done", "fetched", "inserted", "candidates", "indexed", "matched", "scored", "drafted", "errors", "updated_at")
    STAGE_FIELD_NUMBER: _ClassVar[int]
    SOURCES_TOTAL_FIELD_NUMBER: _ClassVar[int]
    SOURCES_DONE_FIELD_NUMBER: _ClassVar[int]
    FETCHED_FIELD_NUMBER: _ClassVar[int]
    INSERTED_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    INDEXED_FIELD_NUMBER: _ClassVar[int]
    MATCHED_FIELD_NUMBER: _ClassVar[int]
    SCORED_FIELD_NUMBER: _ClassVar[int]
    DRAFTED_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    stage: Stage
    sources_total: int
    sources_done: int
    fetched: int
    inserted: int
    candidates: int
    indexed: int
    matched: int
    scored: int
    drafted: int
    errors: _containers.RepeatedScalarFieldContainer[str]
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, stage: _Optional[_Union[Stage, str]] = ..., sources_total: _Optional[int] = ..., sources_done: _Optional[int] = ..., fetched: _Optional[int] = ..., inserted: _Optional[int] = ..., candidates: _Optional[int] = ..., indexed: _Optional[int] = ..., matched: _Optional[int] = ..., scored: _Optional[int] = ..., drafted: _Optional[int] = ..., errors: _Optional[_Iterable[str]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ScoredJob(_message.Message):
    __slots__ = ("job_uid", "company", "title", "location", "url", "score", "pitch_angle", "outbox_id")
    JOB_UID_FIELD_NUMBER: _ClassVar[int]
    COMPANY_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    PITCH_ANGLE_FIELD_NUMBER: _ClassVar[int]
    OUTBOX_ID_FIELD_NUMBER: _ClassVar[int]
    job_uid: str
    company: str
    title: str
    location: str
    url: str
    score: float
    pitch_angle: str
    outbox_id: int
    def __init__(self, job_uid: _Optional[str] = ..., company: _Optional[str] = ..., title: _Optional[str] = ..., location: _Optional[str] = ..., url: _Optional[str] = ..., score: _Optional[float] = ..., pitch_angle: _Optional[str] = ..., outbox_id: _Optional[int] = ...) -> None: ...

class RadarResult(_message.Message):
    __slots__ = ("progress", "top")
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    TOP_FIELD_NUMBER: _ClassVar[int]
    progress: RadarProgress
    top: _containers.RepeatedCompositeFieldContainer[ScoredJob]
    def __init__(self, progress: _Optional[_Union[RadarProgress, _Mapping]] = ..., top: _Optional[_Iterable[_Union[ScoredJob, _Mapping]]] = ...) -> None: ...

class FetchSourceInput(_message.Message):
    __slots__ = ("source",)
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    source: Source
    def __init__(self, source: _Optional[_Union[Source, _Mapping]] = ...) -> None: ...

class FetchSourceResult(_message.Message):
    __slots__ = ("source", "fetched", "inserted")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    FETCHED_FIELD_NUMBER: _ClassVar[int]
    INSERTED_FIELD_NUMBER: _ClassVar[int]
    source: str
    fetched: int
    inserted: int
    def __init__(self, source: _Optional[str] = ..., fetched: _Optional[int] = ..., inserted: _Optional[int] = ...) -> None: ...

class PrefilterInput(_message.Message):
    __slots__ = ("profile_id",)
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    def __init__(self, profile_id: _Optional[str] = ...) -> None: ...

class PrefilterResult(_message.Message):
    __slots__ = ("candidates", "filtered", "duplicates")
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    FILTERED_FIELD_NUMBER: _ClassVar[int]
    DUPLICATES_FIELD_NUMBER: _ClassVar[int]
    candidates: int
    filtered: int
    duplicates: int
    def __init__(self, candidates: _Optional[int] = ..., filtered: _Optional[int] = ..., duplicates: _Optional[int] = ...) -> None: ...

class IndexJobsInput(_message.Message):
    __slots__ = ("batch_size", "profile_id")
    BATCH_SIZE_FIELD_NUMBER: _ClassVar[int]
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    batch_size: int
    profile_id: str
    def __init__(self, batch_size: _Optional[int] = ..., profile_id: _Optional[str] = ...) -> None: ...

class IndexJobsResult(_message.Message):
    __slots__ = ("indexed",)
    INDEXED_FIELD_NUMBER: _ClassVar[int]
    indexed: int
    def __init__(self, indexed: _Optional[int] = ...) -> None: ...

class MatchJobsInput(_message.Message):
    __slots__ = ("profile_id", "top_k")
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    top_k: int
    def __init__(self, profile_id: _Optional[str] = ..., top_k: _Optional[int] = ...) -> None: ...

class MatchJobsResult(_message.Message):
    __slots__ = ("job_uids",)
    JOB_UIDS_FIELD_NUMBER: _ClassVar[int]
    job_uids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, job_uids: _Optional[_Iterable[str]] = ...) -> None: ...

class ScoreJobInput(_message.Message):
    __slots__ = ("profile_id", "job_uid")
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_UID_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    job_uid: str
    def __init__(self, profile_id: _Optional[str] = ..., job_uid: _Optional[str] = ...) -> None: ...

class ScoreJobResult(_message.Message):
    __slots__ = ("job_uid", "score", "scored_by")
    JOB_UID_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    SCORED_BY_FIELD_NUMBER: _ClassVar[int]
    job_uid: str
    score: float
    scored_by: str
    def __init__(self, job_uid: _Optional[str] = ..., score: _Optional[float] = ..., scored_by: _Optional[str] = ...) -> None: ...

class DraftApplicationInput(_message.Message):
    __slots__ = ("profile_id", "job_uid")
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_UID_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    job_uid: str
    def __init__(self, profile_id: _Optional[str] = ..., job_uid: _Optional[str] = ...) -> None: ...

class DraftApplicationResult(_message.Message):
    __slots__ = ("outbox_id", "channel", "critique_score", "revisions")
    OUTBOX_ID_FIELD_NUMBER: _ClassVar[int]
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    CRITIQUE_SCORE_FIELD_NUMBER: _ClassVar[int]
    REVISIONS_FIELD_NUMBER: _ClassVar[int]
    outbox_id: int
    channel: str
    critique_score: float
    revisions: int
    def __init__(self, outbox_id: _Optional[int] = ..., channel: _Optional[str] = ..., critique_score: _Optional[float] = ..., revisions: _Optional[int] = ...) -> None: ...

class TopJobsInput(_message.Message):
    __slots__ = ("profile_id", "limit")
    PROFILE_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    profile_id: str
    limit: int
    def __init__(self, profile_id: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class TopJobsResult(_message.Message):
    __slots__ = ("jobs",)
    JOBS_FIELD_NUMBER: _ClassVar[int]
    jobs: _containers.RepeatedCompositeFieldContainer[ScoredJob]
    def __init__(self, jobs: _Optional[_Iterable[_Union[ScoredJob, _Mapping]]] = ...) -> None: ...

class ApprovalSignal(_message.Message):
    __slots__ = ("outbox_id", "approve", "edited_body")
    OUTBOX_ID_FIELD_NUMBER: _ClassVar[int]
    APPROVE_FIELD_NUMBER: _ClassVar[int]
    EDITED_BODY_FIELD_NUMBER: _ClassVar[int]
    outbox_id: int
    approve: bool
    edited_body: str
    def __init__(self, outbox_id: _Optional[int] = ..., approve: bool = ..., edited_body: _Optional[str] = ...) -> None: ...
