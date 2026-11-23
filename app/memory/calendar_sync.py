"""RFC 5545-lite ICS exchange for the local calendar."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from . import repo
from .db import connect


def _escape(value: str) -> str:
    return str(value or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def _unescape(value: str) -> str:
    return value.replace('\\n', '\n').replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')


def export_ics(course: str | None = None, workspace: str | None = None) -> str:
    rows = repo.list_events(course, workspace)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//CyberPal//Calendar//CN', 'CALSCALE:GREGORIAN']
    for event in rows:
        uid = event.get('source_uid') or event.get('record_id') or f"cyberpal-{event['id']}@local"
        date = str(event.get('date') or '').replace('-', '')
        if not re.fullmatch(r'\d{8}', date):
            continue
        lines.extend(['BEGIN:VEVENT', f'UID:{_escape(uid)}', f'DTSTART;VALUE=DATE:{date}',
                      f'SUMMARY:{_escape(event.get("title", ""))}',
                      f'CATEGORIES:{_escape(event.get("category", "personal"))}'])
        if event.get('course_name'):
            lines.append(f'X-CYBERPAL-COURSE:{_escape(event["course_name"])}')
        if event.get('time_range'):
            lines.append(f'DESCRIPTION:{_escape(event.get("time_range", ""))} {_escape(event.get("note", ""))}')
        elif event.get('note'):
            lines.append(f'DESCRIPTION:{_escape(event["note"])}')
        if event.get('location'):
            lines.append(f'LOCATION:{_escape(event["location"])}')
        lines.extend(['END:VEVENT'])
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'


def _unfold(text: str) -> list[str]:
    out = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line.startswith((' ', '\t')) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def import_ics(text: str, origin: str = 'ics', workspace: str = 'studypal') -> dict:
    if len(text.encode('utf-8')) > 10 * 1024 * 1024:
        raise ValueError('ICS 文件不能超过 10 MB')
    events = []
    current = None
    for line in _unfold(text):
        if line == 'BEGIN:VEVENT':
            current = {}
        elif line == 'END:VEVENT' and current is not None:
            events.append(current)
            current = None
        elif current is not None and ':' in line:
            key, value = line.split(':', 1)
            key = key.split(';', 1)[0].upper()
            current[key] = _unescape(value)
    created = updated = conflicts = 0
    for item in events[:2000]:
        raw_date = item.get('DTSTART', '')[:8]
        if not re.fullmatch(r'\d{8}', raw_date):
            continue
        date = f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}'
        uid = item.get('UID') or f'ics-{uuid.uuid4()}@import'
        course = item.get('X-CYBERPAL-COURSE', '')
        description = item.get('DESCRIPTION', '')
        values = {'record_id': uid, 'source': 'ics', 'source_uid': uid,
                  'source_updated_at': item.get('LAST-MODIFIED', ''),
                  'course_name': course, 'title': item.get('SUMMARY', '(未命名事项)'),
                  'category': item.get('CATEGORIES', 'personal').split(',')[0].lower(),
                  'date': date, 'location': item.get('LOCATION', ''), 'note': description}
        target = repo.get_workspace(workspace)
        if not target:
            raise ValueError('工作区不存在')
        values['workspace_id'] = target['id']
        if values['category'] not in {'class', 'hw', 'exam', 'personal'}:
            values['category'] = 'personal'
        conn = connect()
        try:
            existing = conn.execute('SELECT * FROM events WHERE source_uid=? AND workspace_id=?',
                                     (uid, target['id'])).fetchone()
            if not existing:
                repo.create_event(values, origin=origin)
                created += 1
                continue
            incoming = dict(values)
            local = dict(existing)
            comparable = ('title', 'category', 'date', 'location', 'note', 'course_name')
            changed = any(str(local.get(key) or '') != str(incoming.get(key) or '') for key in comparable)
            if not changed:
                continue
            with conn:
                conn.execute('''INSERT INTO event_sync_conflicts(event_id,source_uid,local_json,incoming_json)
                                VALUES(?,?,?,?)''', (existing['id'], uid, json.dumps(local, ensure_ascii=False), json.dumps(incoming, ensure_ascii=False)))
            conflicts += 1
        finally:
            conn.close()
    return {'ok': True, 'created': created, 'updated': updated, 'conflicts': conflicts, 'seen': len(events[:2000])}


def conflicts(status: str = 'pending', workspace: str | None = None) -> list[dict]:
    conn = connect()
    try:
        return [dict(row) for row in conn.execute('''SELECT sc.* FROM event_sync_conflicts sc
            JOIN events e ON e.id=sc.event_id LEFT JOIN workspaces w ON w.id=e.workspace_id
            WHERE sc.status=? AND (? IS NULL OR COALESCE(w.key,'studypal')=?)
            ORDER BY sc.id DESC''', (status,workspace,workspace)).fetchall()]
    finally:
        conn.close()


def resolve_conflict(conflict_id: int, choice: str) -> dict:
    if choice not in {'local', 'incoming'}:
        raise ValueError('冲突处理只能选择 local 或 incoming')
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM event_sync_conflicts WHERE id=? AND status="pending"', (conflict_id,)).fetchone()
        if not row:
            raise ValueError('待处理冲突不存在')
        payload = json.loads(row['incoming_json']) if choice == 'incoming' else json.loads(row['local_json'])
        if choice == 'incoming':
            repo.update_event(row['event_id'], payload, origin='ics-conflict')
        conn.execute('UPDATE event_sync_conflicts SET status=?,resolved_at=datetime(\'now\',\'localtime\') WHERE id=?',
                     ('accepted_incoming' if choice == 'incoming' else 'accepted_local', conflict_id))
        conn.commit()
        return {'ok': True, 'choice': choice, 'event_id': row['event_id']}
    finally:
        conn.close()
