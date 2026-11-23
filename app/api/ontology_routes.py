"""Ontology contracts, mapping preflight, and local human review of saved mappings."""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..agent.llm import LLMError
from ..memory import mapping_assistant, mapping_extraction, ontology, ontology_mappings, repo
from .mcp_routes import local_only

router = APIRouter(prefix='/knowledge/ontology')


class MappingCheck(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    workspace_key: str = Field(min_length=1, max_length=48)
    mapping: dict[str, Any] = Field(max_length=20)


@router.get('')
def get_contract() -> dict:
    return ontology.public_contract()


@router.get('/mapping-schema')
def get_mapping_schema() -> dict:
    return ontology.mapping_json_schema()


@router.post('/validate-mapping')
def validate_mapping(body: MappingCheck) -> dict:
    workspace = repo.get_workspace(body.workspace_key)
    if workspace is None:
        raise HTTPException(404, '工作区不存在或已归档')
    try:
        mapping = ontology.validate_mapping(body.mapping, workspace_kind=workspace['kind'])
    except ValueError:
        # Validation errors can contain private source fields supplied by callers.
        raise HTTPException(422, '映射不符合当前本体、领域版本或工作区约束，请核对合同后重试') from None
    return {
        'workspace_key': workspace['key'], 'mapping': mapping,
        'validation_only': True, 'semantic_support_checked': False,
        'sample_sources_verified': False,
    }


_MAPPING_INVALID = '映射请求无效，请检查资料、用途、本体及工作区约束后重试'
_WorkspaceKey = Annotated[str, Field(min_length=1, max_length=48)]
_VersionId = Annotated[str, Path(pattern=r'^[1-9][0-9]*$', max_length=19)]


class MappingCreate(MappingCheck):
    base_version_id: Annotated[int, Field(gt=0, le=9223372036854775807)] | None = None


class MappingSuggestion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    workspace_key: _WorkspaceKey
    source_id: Annotated[int, Field(gt=0, le=9223372036854775807)]
    profile_id: str = Field(min_length=1, max_length=256)
    purpose: str = Field(min_length=1, max_length=2000)


class MappingCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    workspace_key: _WorkspaceKey
    expected_hash: str = Field(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)


class MappingReview(MappingCommand):
    note: str = Field(default='', max_length=2000)


class MappingApproval(MappingReview):
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def require_boolean_true(cls, value):
        # Literal[True] alone accepts 1, even with strict model configuration.
        if value is not True:
            raise ValueError('Explicit approval is required')
        return value


class _MappingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized_handler(request: Request):
            try:
                if request.method == 'POST' and request.url.path.endswith('/mappings/suggest'):
                    local_only(request)
                    raw = await request.body()
                    if len(raw) > 65536:
                        raise ValueError('Oversized mapping request')
                    # FastAPI's normal JSON decoder silently accepts duplicate
                    # keys and non-finite numbers before Pydantic sees them.
                    json.loads(raw, object_pairs_hook=mapping_assistant._unique_object,
                               parse_constant=mapping_assistant._invalid_constant)
                return await handler(request)
            except (RequestValidationError, ValueError, RecursionError):
                # FastAPI's default validation response includes caller input.
                raise HTTPException(422, _MAPPING_INVALID) from None

        return sanitized_handler


def _mapping_call(operation, *args, workspace_key, **kwargs):
    if repo.get_workspace(workspace_key) is None:
        raise HTTPException(404, '资料、映射或工作区不存在或不可用')
    try:
        return operation(*args, workspace_key=workspace_key, **kwargs)
    except ontology_mappings.MappingNotFound:
        raise HTTPException(404, '资料、映射或工作区不存在或不可用') from None
    except ontology_mappings.MappingConflict:
        raise HTTPException(409, '资料、规则或审核状态已变化，请刷新并重新生成或审核') from None
    except LLMError:
        raise HTTPException(502, '模型暂不可用或响应超时，请检查设置中的 fast 模型配置后重试') from None
    except ValueError:
        raise HTTPException(422, _MAPPING_INVALID) from None


_mapping_router = APIRouter(prefix='/mappings', route_class=_MappingRoute,
                            dependencies=[Depends(local_only)])


@_mapping_router.get('')
def list_mappings(
    workspace_key: Annotated[str, Query(min_length=1, max_length=48)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[dict]:
    return _mapping_call(ontology_mappings.list_mappings, workspace_key=workspace_key, limit=limit)


@_mapping_router.get('/inputs')
def mapping_inputs(workspace_key: Annotated[str, Query(min_length=1, max_length=48)]) -> dict:
    return _mapping_call(mapping_assistant.get_inputs, workspace_key=workspace_key)


@_mapping_router.get('/samples')
def mapping_samples(
    workspace_key: Annotated[str, Query(min_length=1, max_length=48)],
    source_id: Annotated[int, Query(gt=0, le=9223372036854775807)],
) -> dict:
    return _mapping_call(mapping_assistant.get_samples, source_id, workspace_key=workspace_key)


@_mapping_router.post('/suggest')
def suggest_mapping(body: MappingSuggestion) -> dict:
    return _mapping_call(
        mapping_assistant.suggest_mapping, workspace_key=body.workspace_key,
        source_id=body.source_id, profile_id=body.profile_id, purpose=body.purpose,
    )


@_mapping_router.get('/{version_id}/evidence')
def mapping_evidence(
    version_id: _VersionId,
    workspace_key: Annotated[str, Query(min_length=1, max_length=48)],
) -> dict:
    return _mapping_call(mapping_assistant.get_evidence, int(version_id), workspace_key=workspace_key)


@_mapping_router.get('/{version_id}')
def get_mapping(
    version_id: _VersionId,
    workspace_key: Annotated[str, Query(min_length=1, max_length=48)],
) -> dict:
    return _mapping_call(ontology_mappings.get_mapping, int(version_id), workspace_key=workspace_key)


@_mapping_router.post('')
def create_mapping(body: MappingCreate) -> dict:
    return _mapping_call(
        ontology_mappings.create_mapping, workspace_key=body.workspace_key, mapping=body.mapping,
        created_by='local_user', base_version_id=body.base_version_id,
    )


@_mapping_router.post('/{version_id}/submit')
def submit_mapping(version_id: _VersionId, body: MappingCommand) -> dict:
    return _mapping_call(
        ontology_mappings.submit_mapping, int(version_id),
        workspace_key=body.workspace_key, expected_hash=body.expected_hash,
    )


@_mapping_router.post('/{version_id}/approve')
def approve_mapping(version_id: _VersionId, body: MappingApproval) -> dict:
    return _mapping_call(
        ontology_mappings.approve_mapping, int(version_id), approved_by='local_user', note=body.note,
        workspace_key=body.workspace_key, expected_hash=body.expected_hash,
    )


@_mapping_router.post('/{version_id}/reject')
def reject_mapping(version_id: _VersionId, body: MappingReview) -> dict:
    return _mapping_call(
        ontology_mappings.reject_mapping, int(version_id), note=body.note,
        workspace_key=body.workspace_key, expected_hash=body.expected_hash,
    )


router.include_router(_mapping_router)


_EXTRACTION_INVALID = '抽取请求无效，请核对工作区、已批准规则及资料范围后重试'
_PositiveId = Annotated[int, Field(gt=0, le=9223372036854775807)]


class ExtractionCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    workspace_key: _WorkspaceKey


class ExtractionCreate(ExtractionCommand):
    mapping_version_id: _PositiveId
    mapping_hash: str = Field(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)
    source_ids: list[_PositiveId] = Field(min_length=1, max_length=64)

    @field_validator('source_ids')
    @classmethod
    def exact_source_set(cls, value):
        if value != sorted(set(value)):
            raise ValueError('Expected sorted unique source IDs')
        return value


class _ExtractionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized_handler(request: Request):
            local_only(request)
            try:
                if request.method == 'POST':
                    raw = await request.body()
                    if len(raw) > 65536:
                        raise ValueError('Oversized extraction request')
                    json.loads(raw, object_pairs_hook=mapping_assistant._unique_object,
                               parse_constant=mapping_assistant._invalid_constant)
                return await handler(request)
            except (RequestValidationError, ValueError, RecursionError):
                raise HTTPException(422, _EXTRACTION_INVALID) from None

        return sanitized_handler


def _extraction_call(operation, *args, workspace_key, **kwargs):
    if repo.get_workspace(workspace_key) is None:
        raise HTTPException(404, '抽取任务、资料或工作区不存在或不可用')
    try:
        return operation(*args, workspace_key=workspace_key, **kwargs)
    except mapping_extraction.ExtractionNotFound:
        raise HTTPException(404, '抽取任务、资料或工作区不存在或不可用') from None
    except (mapping_extraction.ExtractionConflict, OSError):
        raise HTTPException(409, '资料、索引或批准规则已变化，请刷新后重新核对') from None
    except LLMError:
        raise HTTPException(502, '模型暂不可用或响应超时，请检查 fast 模型配置后重试') from None
    except ValueError:
        raise HTTPException(422, _EXTRACTION_INVALID) from None


_extraction_router = APIRouter(prefix='/extractions', route_class=_ExtractionRoute,
                               dependencies=[Depends(local_only)])


@_extraction_router.post('')
def create_extraction(body: ExtractionCreate) -> dict:
    return _extraction_call(mapping_extraction.create_run, **body.model_dump())


@_extraction_router.get('')
def list_extractions(
    workspace_key: Annotated[str, Query(min_length=1, max_length=48)],
    mapping_version_id: Annotated[int, Query(gt=0, le=9223372036854775807)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    return _extraction_call(mapping_extraction.list_runs, workspace_key=workspace_key,
                            mapping_version_id=mapping_version_id, limit=limit)


@_extraction_router.get('/{run_id}')
def get_extraction(run_id: _VersionId,
                   workspace_key: Annotated[str, Query(min_length=1, max_length=48)]) -> dict:
    return _extraction_call(mapping_extraction.get_run, int(run_id), workspace_key=workspace_key)


@_extraction_router.post('/{run_id}/step')
def step_extraction(run_id: _VersionId, body: ExtractionCommand) -> dict:
    return _extraction_call(mapping_extraction.step_run, int(run_id), workspace_key=body.workspace_key)


@_extraction_router.post('/{run_id}/cancel')
def cancel_extraction(run_id: _VersionId, body: ExtractionCommand) -> dict:
    return _extraction_call(mapping_extraction.cancel_run, int(run_id), workspace_key=body.workspace_key)


@_extraction_router.post('/{run_id}/retry')
def retry_extraction(run_id: _VersionId, body: ExtractionCommand) -> dict:
    return _extraction_call(mapping_extraction.retry_run, int(run_id), workspace_key=body.workspace_key)


router.include_router(_extraction_router)
