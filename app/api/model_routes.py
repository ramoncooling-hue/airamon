"""Local JSON management for the saved model catalog. Registered by the caller."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.settings import model_profiles as models, store
from .mcp_routes import local_only

router = APIRouter(prefix='/settings/models', dependencies=[Depends(local_only)])


def safe_call(fn, *args):
    try:
        return fn(*args)
    except store.RevisionConflict:
        raise HTTPException(409, '配置已变更，请重新加载后再保存') from None
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        raise HTTPException(422, '模型配置无效，请检查模型列表、接口地址与档位绑定') from None


@router.get('')
def get_config():
    return safe_call(models.read)


@router.put('')
async def put_config(request: Request):
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise HTTPException(415, '请使用 JSON 配置')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > models.MAX_CONFIG:
            raise HTTPException(413, '模型配置请求超出大小限制')
    try:
        value = json.loads(raw, object_pairs_hook=store._json_object, parse_constant=store._invalid_constant)
        if not isinstance(value, dict) or value.keys() != {'profiles', 'routes', 'revision'}:
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, '模型配置请求格式无效') from None
    return await run_in_threadpool(safe_call, models.save, value['profiles'], value['routes'], value['revision'])
