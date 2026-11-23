"""Loadable, portable domain profiles for the reusable knowledge graph."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..runtime import ROOT
from .knowledge import NODE_TYPES, RELATION_KINDS

PROFILE_DIR = ROOT / 'domains'
REQUIRED_KEYS = {'id', 'version', 'workspace', 'display_name', 'node_types',
                 'relation_kinds', 'required_fields', 'quality_rules', 'wiki_rules'}
VERSION_PATTERN = re.compile(
    r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
    r'(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
)


def _string_list(profile: dict[str, Any], key: str, path: Path, *, optional: bool = False) -> list[str]:
    values = profile.get(key, [])
    if (not isinstance(values, list) or (not optional and not values)
            or not all(isinstance(item, str) and item.strip() for item in values)):
        raise ValueError(f'{path.name} 的 {key} 必须是非空字符串数组')
    normalized = [item.strip().casefold() if key == 'course_patterns' else item.strip() for item in values]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f'{path.name} 的 {key} 含有重复项')
    return values


def _validate(profile: dict[str, Any], path: Path) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError(f'{path.name} 的领域档案必须是对象')
    missing = sorted(REQUIRED_KEYS - set(profile))
    if missing:
        raise ValueError(f'{path.name} 缺少领域档案字段：{", ".join(missing)}')
    if not isinstance(profile['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{1,40}', profile['id']):
        raise ValueError(f'{path.name} 的 id 不合法')
    version = profile['version']
    match = VERSION_PATTERN.fullmatch(version) if isinstance(version, str) else None
    if not match or any(part.isdigit() and len(part) > 1 and part.startswith('0')
                        for part in (match['prerelease'] or '').split('.')):
        raise ValueError(f'{path.name} 的 version 必须是语义版本号')
    for key in ('workspace', 'display_name'):
        value = profile[key]
        if not isinstance(value, str) or (not value.strip() and (key != 'workspace' or value != '')):
            raise ValueError(f'{path.name} 的 {key} 必须是非空字符串（workspace 可为空串）')
    if 'workspace_default' in profile and not isinstance(profile['workspace_default'], bool):
        raise ValueError(f'{path.name} 的 workspace_default 必须是布尔值')
    if profile.get('workspace_default') and not profile['workspace'].strip():
        raise ValueError(f'{path.name} 的 workspace_default 必须指定 workspace')
    for key, allowed in (('node_types', NODE_TYPES), ('relation_kinds', RELATION_KINDS)):
        values = _string_list(profile, key, path)
        if not all(item in allowed for item in values):
            raise ValueError(f'{path.name} 的 {key} 含有未注册类型')
    if 'source_refs' not in _string_list(profile, 'required_fields', path):
        raise ValueError(f'{path.name} 必须要求 source_refs')
    _string_list(profile, 'quality_rules', path)
    _string_list(profile, 'course_patterns', path, optional=True)
    _string_list(profile, 'extraction_sections', path, optional=True)
    if not isinstance(profile['wiki_rules'], dict):
        raise ValueError(f'{path.name} 的 wiki_rules 必须是对象')
    for key in ('frontmatter_schema', 'link_style', 'evidence_locator'):
        value = profile['wiki_rules'].get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{path.name} 的 wiki_rules.{key} 必须是非空字符串')
    return profile


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'重复 JSON 字段：{key}')
        result[key] = value
    return result


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    workspaces: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(PROFILE_DIR.glob('*.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
        except ValueError as exc:
            raise ValueError(f'{path.name} 的 JSON 无效：{exc}') from exc
        profile = _validate(payload, path)
        profile_id = profile['id']
        if profile_id in profiles:
            raise ValueError(f'{path.name} 与 {paths[profile_id].name} 的领域 id 重复：{profile_id}')
        profiles[profile_id] = profile
        paths[profile_id] = path
        workspace = profile['workspace'].strip().casefold()
        if workspace:
            workspaces.setdefault(workspace, []).append(profile)
    if not profiles:
        raise ValueError('没有可用的领域档案')
    for workspace, candidates in workspaces.items():
        if len(candidates) > 1 and sum(bool(item.get('workspace_default')) for item in candidates) != 1:
            names = ', '.join(paths[item['id']].name for item in reversed(candidates))
            raise ValueError(f'{names} 共用 {workspace}，必须且只能指定一个 workspace_default')
    return profiles


def profile_for_course(course: str = '', workspace: str = '') -> dict[str, Any]:
    """Select domain rules; profile workspace hints do not establish ownership."""
    profiles = load_profiles()
    selector = workspace.strip().casefold()
    value = course.casefold()
    candidates = list(profiles.values())
    if selector:
        candidates = [profile for profile in candidates
                      if profile['workspace'].strip().casefold() == selector]
        if not candidates:
            # Wiki writers pass the selected domain id back through this legacy argument.
            if selector in profiles:
                return profiles[selector]
            if 'general' not in profiles:
                raise ValueError('未知工作区需要通用领域档案 general')
            return profiles['general']
    for profile in candidates:
        if any(pattern.strip().casefold() in value for pattern in profile.get('course_patterns', [])):
            return profile
    if selector:
        return next((profile for profile in candidates if profile.get('workspace_default')), candidates[0])
    # Course-only callers predate personal workspaces and retain the MBA default.
    return profiles.get('mba') or next(iter(profiles.values()))


def public_profile(course: str = '', workspace: str = '') -> dict[str, Any]:
    profile = profile_for_course(course, workspace)
    return {
        'id': profile['id'], 'version': profile['version'],
        'workspace': profile['workspace'], 'display_name': profile['display_name'],
        'node_types': profile['node_types'], 'relation_kinds': profile['relation_kinds'],
        'required_fields': profile['required_fields'], 'quality_rules': profile['quality_rules'],
        'extraction_sections': profile.get('extraction_sections', []),
        'wiki_rules': profile['wiki_rules'],
    }


def clear_cache() -> None:
    load_profiles.cache_clear()
