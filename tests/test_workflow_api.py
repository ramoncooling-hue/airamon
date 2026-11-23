import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api import routes
from app.hub import registry
from app.memory import db


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / 'skills'
        self.agents = self.root / 'agents'
        self.skills.mkdir()
        self.agents.mkdir()
        self.patches = [
            patch.object(db, 'APP_DIR', self.root / 'app'),
            patch.object(db, 'DB_PATH', self.root / 'app' / 'test.db'),
            patch.object(db, 'DATA_DIR', self.root / 'data'),
            patch.object(registry, 'SKILLS_DIR', self.skills),
            patch.object(registry, 'AGENTS_DIR', self.agents),
            patch('app.runtime.data_dir', return_value=self.root),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        db.apply_schema()
        self.add_hub_entry('agent', 'laoba')
        self.add_hub_entry('agent', 'paper')
        self.add_hub_entry('skill', 'web-research')
        self.add_hub_entry('skill', 'local-research')

    def add_hub_entry(self, kind, key, enabled=True, skills=None):
        directory = (self.skills if kind == 'skill' else self.agents) / key
        directory.mkdir()
        content_name = 'SKILL.md' if kind == 'skill' else 'system.md'
        (directory / content_name).write_text(f'# {key}\n', encoding='utf-8')
        (directory / 'manifest.json').write_text(json.dumps({
            'key': key, 'name': key, 'version': '1.0.0', 'skills': skills or []
        }), encoding='utf-8')
        registry.sync()
        if not enabled:
            registry.set_enabled(kind, key, False)

    def create(self, **kwargs):
        return routes.workflow_create(routes.WorkflowCreateIn(**kwargs))

    def test_create_paper_template_and_edit_steps(self):
        created = self.create(key='paper-api', name='论文整理', template='paper_research')
        self.assertEqual([step['kind'] for step in created['steps']],
                         ['input', 'skill', 'skill', 'approval', 'agent', 'approval'])
        self.assertNotIn('prompt', json.dumps(created, ensure_ascii=False))
        updated = routes.workflow_step_update(
            'paper-api', 'topic', routes.WorkflowStepUpdateIn(name='论文主题'))
        self.assertEqual(updated['name'], '论文主题')
        reordered = routes.workflow_steps_reorder(
            'paper-api', routes.WorkflowReorderIn(ordered_keys=[
                'topic', 'web_research', 'local_research', 'select_evidence', 'draft', 'final_review'
            ]))
        self.assertEqual(reordered[0]['key'], 'topic')

    def test_run_waiting_approval_approve_cancel_and_retry(self):
        self.create(key='approval-api', name='审批流程')
        routes.workflow_step_create('approval-api', routes.WorkflowStepCreateIn(
            key='input', kind='input', name='输入'))
        routes.workflow_step_create('approval-api', routes.WorkflowStepCreateIn(
            key='approval', kind='approval', name='确认'))
        waiting = routes.workflow_run_start('approval-api', routes.WorkflowRunIn(input_data={'question': 'q'}))
        self.assertEqual(waiting['status'], 'waiting_approval')
        approved = routes.workflow_run_approve(waiting['id'], routes.WorkflowApprovalIn(decision='approved'))
        self.assertEqual(approved['status'], 'success')

        cancelled = routes.workflow_run_start('approval-api', routes.WorkflowRunIn(input_data={'question': 'q'}))
        cancelled = routes.workflow_run_cancel(cancelled['id'])
        self.assertEqual(cancelled['status'], 'cancelled')

        self.create(key='retry-api', name='重试流程')
        routes.workflow_step_create('retry-api', routes.WorkflowStepCreateIn(
            key='agent', kind='agent', name='执行', agent_key='laoba'))
        with patch.object(routes.agent_chat, 'answer', side_effect=[RuntimeError('failed'), {'ok': True, 'text': 'done'}]):
            failed = routes.workflow_run_start('retry-api', routes.WorkflowRunIn(input_data={'question': 'q'}))
            self.assertEqual(failed['status'], 'error')
            retried = routes.workflow_run_retry(failed['id'])
        self.assertEqual(retried['status'], 'success')

    def test_disabled_dependency_export_redaction_and_import_validation(self):
        self.add_hub_entry('agent', 'disabled-agent', enabled=False)
        self.create(key='bad-api', name='坏依赖')
        with self.assertRaises(HTTPException) as error:
            routes.workflow_step_create('bad-api', routes.WorkflowStepCreateIn(
                key='agent', kind='agent', name='执行', agent_key='disabled-agent'))
        self.assertEqual(error.exception.status_code, 400)

        self.create(key='export-api', name='导出')
        from app.workflows.service import WorkflowService
        WorkflowService().add_step('export-api', {
            'key': 'input', 'kind': 'input', 'name': '输入',
            'config': {'prompt': 'private prompt', 'model': 'private-model', 'web_mode': 'off'},
        })
        exported = routes.workflow_export('export-api')
        serialized = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn('private prompt', serialized)
        self.assertNotIn('private-model', serialized)
        self.assertNotIn('api_key', serialized)

        payload = dict(exported)
        payload['key'] = 'import-unknown'
        payload['steps'] = [{
            'key': 'agent', 'kind': 'agent', 'name': '未知', 'agent_key': 'missing-agent',
            'skill_key': '', 'config': {}, 'input_map': {}, 'output_schema': {}, 'on_error': 'pause',
        }]
        with self.assertRaises(HTTPException) as error:
            routes.workflow_import(routes.WorkflowImportIn(**payload))
        self.assertEqual(error.exception.status_code, 400)

        payload['key'] = 'import-ok'
        payload['steps'] = [{
            'key': 'agent', 'kind': 'agent', 'name': '已知', 'agent_key': 'laoba',
            'skill_key': '', 'config': {}, 'input_map': {}, 'output_schema': {}, 'on_error': 'pause',
        }]
        imported = routes.workflow_import(routes.WorkflowImportIn(**payload))
        self.assertEqual(imported['key'], 'import-ok')

    def test_skill_create_api_returns_file_backed_skill(self):
        result = routes.hub_skill_create(routes.HubSkillIn(
            key='new-skill', name='新技能', version='1.0.0', description='可复用步骤',
            content='# 新技能\n先确认输入。'))
        self.assertEqual(result['key'], 'new-skill')
        self.assertEqual(result['content'], '# 新技能\n先确认输入。')
        self.assertTrue((self.skills / 'new-skill' / 'SKILL.md').exists())

    def test_http_400_404_mapping_and_secret_payload_rejection(self):
        with self.assertRaises(HTTPException) as error:
            routes.workflow_get('missing')
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            routes.workflow_run_cancel(999999)
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            self.create(key='bad key', name='坏')
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as error:
            self.create(key='secret-api', name='秘密', trigger={'api_key': 'do-not-store'})
        self.assertEqual(error.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
