import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hub import registry
from app.memory import db
from app import workflows
from app.workflows import service as workflow_service
from app.agent import chat, llm


class WorkflowSchemaTests(unittest.TestCase):
    def test_schema_applies_and_enforces_workflow_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, 'APP_DIR', root / 'app'), patch.object(db, 'DB_PATH', root / 'app' / 'test.db'):
                applied = db.apply_schema()
                applied_again = db.apply_schema()
                self.assertIn('017_workflows.sql', applied)
                self.assertIn('017_workflows.sql', applied_again)
                conn = db.connect()
                try:
                    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertTrue({'workflows', 'workflow_steps', 'workflow_runs', 'workflow_run_steps'} <= tables)
                    table_counts = {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                                    for name in ('workflows', 'workflow_steps', 'workflow_runs', 'workflow_run_steps')}
                    self.assertEqual(table_counts, {
                        'workflows': 0, 'workflow_steps': 0, 'workflow_runs': 0, 'workflow_run_steps': 0,
                    })
                    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                    self.assertTrue({'idx_workflow_steps_order', 'idx_workflow_runs_status',
                                     'idx_workflow_run_steps_status'} <= indexes)
                    conn.execute("INSERT INTO workflows(key,name,status) VALUES('demo','Demo','draft')")
                    workflow_id = conn.execute("SELECT id FROM workflows WHERE key='demo'").fetchone()[0]
                    conn.execute("INSERT INTO workflow_steps(workflow_id,key,position,kind) VALUES(?,?,?,?)",
                                 (workflow_id, 'one', 0, 'input'))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_steps(workflow_id,key,position,kind) VALUES(?,?,?,?)",
                                     (workflow_id, 'two', 0, 'agent'))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_steps(workflow_id,key,position,kind) VALUES(?,?,?,?)",
                                     (workflow_id, 'one', 1, 'agent'))
                    for column, value in (('status', 'running'),):
                        with self.assertRaises(Exception):
                            conn.execute(f"INSERT INTO workflows(key,name,{column}) VALUES('bad-{column}','Bad',?)", (value,))
                    for value in ('bad-kind',):
                        with self.assertRaises(Exception):
                            conn.execute("INSERT INTO workflow_steps(workflow_id,key,position,kind) VALUES(?,?,?,?)",
                                         (workflow_id, value, 1, value))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_steps(workflow_id,key,position,kind,on_error) VALUES(?,?,?,?,?)",
                                     (workflow_id, 'bad-error', 1, 'input', 'retry'))
                    conn.execute("INSERT INTO workflow_runs(workflow_id,status) VALUES(?,?)", (workflow_id, 'running'))
                    run_id = conn.execute("SELECT id FROM workflow_runs").fetchone()[0]
                    step_id = conn.execute("SELECT id FROM workflow_steps WHERE key='one'").fetchone()[0]
                    conn.execute("INSERT INTO workflow_run_steps(run_id,step_id,status) VALUES(?,?,?)",
                                 (run_id, step_id, 'pending'))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_run_steps(run_id,step_id,status) VALUES(?,?,?)",
                                     (run_id, step_id, 'success'))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_runs(workflow_id,status) VALUES(?,?)", (workflow_id, 'paused'))
                    with self.assertRaises(Exception):
                        conn.execute("INSERT INTO workflow_run_steps(run_id,step_id,status) VALUES(?,?,?)",
                                     (run_id, step_id, 'paused'))
                    conn.commit()
                finally:
                    conn.close()


class WorkflowServiceContractTests(unittest.TestCase):
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
            patch.object(registry, 'SKILLS_DIR', self.skills),
            patch.object(registry, 'AGENTS_DIR', self.agents),
            patch('app.runtime.data_dir', return_value=self.root),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        db.apply_schema()
        self.add_hub_entry('agent', 'laoba')
        self.add_hub_entry('agent', 'agent-a')
        self.add_hub_entry('agent', 'agent-b')

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

    def test_create_and_paper_research_template_have_expected_order_and_skill_refs(self):
        self.add_hub_entry('skill', 'web-research')
        self.add_hub_entry('skill', 'local-research')
        self.add_hub_entry('agent', 'paper', skills=['web-research', 'local-research'])
        service = workflows.WorkflowService()
        created = service.create({'key': 'research', 'name': 'Research'})
        workflow = service.create_from_template('paper_research', workflow_id=created['id'])
        steps = workflow['steps']
        self.assertEqual([step['kind'] for step in steps],
                         ['input', 'skill', 'skill', 'approval', 'agent', 'approval'])
        self.assertEqual([step.get('skill_key') for step in steps[1:3]],
                         ['web-research', 'local-research'])
        self.assertEqual(steps[4].get('agent_key'), 'paper')

    def test_workflow_get_update_delete(self):
        service = workflows.WorkflowService()
        created = service.create({'key': 'crud', 'name': 'Original'})
        self.assertEqual(service.get(created['key'])['name'], 'Original')
        updated = service.update(created['key'], {'name': 'Renamed', 'description': 'Updated'})
        self.assertEqual(updated['name'], 'Renamed')
        self.assertEqual(updated['description'], 'Updated')
        self.assertTrue(service.delete(created['key']))
        with self.assertRaises(ValueError):
            service.get(created['key'])

    def test_edit_and_reorder_steps_keep_positions_contiguous(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'draft', 'name': 'Draft'})
        first = service.add_step(workflow['key'], {'key': 'first', 'kind': 'input', 'name': 'Input'})
        second = service.add_step(workflow['key'], {'key': 'second', 'kind': 'approval', 'name': 'Review'})
        service.update_step(workflow['key'], first['key'], {'name': 'Research question'})
        ordered = service.reorder_steps(workflow['key'], [second['key'], first['key']])
        self.assertEqual([step['position'] for step in ordered], [0, 1])
        self.assertEqual(ordered[1]['name'], 'Research question')

    def test_missing_or_disabled_hub_entry_cannot_be_saved_as_executable_step(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'draft', 'name': 'Draft'})
        with self.assertRaises(ValueError):
            service.add_step(workflow['key'], {'key': 'missing', 'kind': 'agent', 'agent_key': 'missing'})
        self.add_hub_entry('agent', 'disabled-agent', enabled=False)
        with self.assertRaises(ValueError):
            service.add_step(workflow['key'], {'key': 'disabled', 'kind': 'agent', 'agent_key': 'disabled-agent'})
        self.add_hub_entry('skill', 'disabled-skill', enabled=False)
        with self.assertRaises(ValueError):
            service.add_step(workflow['key'], {'key': 'disabled-skill-step', 'kind': 'skill', 'skill_key': 'disabled-skill'})

    def test_run_waits_at_approval_and_resume_does_not_repeat_successful_steps(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'approval-flow', 'name': 'Approval flow'})
        service.add_step(workflow['key'], {'key': 'a', 'kind': 'agent', 'agent_key': 'agent-a'})
        service.add_step(workflow['key'], {'key': 'approve', 'kind': 'approval'})
        service.add_step(workflow['key'], {'key': 'b', 'kind': 'agent', 'agent_key': 'agent-b'})
        with patch('app.workflows.service.agent_chat.answer', return_value={'ok': True, 'text': 'done'}) as answer:
            first = service.run(workflow['key'], {'question': 'research'})
            self.assertEqual(answer.call_count, 1)
            resumed = service.approve(first['id'], 'confirmed')
            self.assertEqual(answer.call_count, 2)
            self.assertEqual(answer.call_args.kwargs['workspace'], 'studypal')
        self.assertEqual(first['status'], 'waiting_approval')
        self.assertEqual(first['step_statuses'], ['success', 'waiting_approval', 'pending'])
        self.assertEqual(resumed['status'], 'success')
        self.assertEqual(resumed['executed_step_keys'].count('a'), 1)

    def test_retry_once_retries_only_the_failed_step(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'retry-flow', 'name': 'Retry flow'})
        service.add_step(workflow['key'], {'key': 'ok', 'kind': 'agent', 'agent_key': 'agent-a'})
        service.add_step(workflow['key'], {'key': 'flaky', 'kind': 'agent', 'agent_key': 'agent-b', 'on_error': 'retry_once'})
        with patch('app.workflows.service.agent_chat.answer', side_effect=[{'ok': True, 'text': 'done'}, RuntimeError('temporary'), {'ok': True, 'text': 'recovered'}]) as answer:
            result = service.run(workflow['key'], {'question': 'research'})
        self.assertEqual(answer.call_count, 3)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['retried_step_keys'], ['flaky'])
        self.assertEqual(result['execution_counts'], {'ok': 1, 'flaky': 2})

    def test_export_excludes_secrets_file_contents_and_run_context(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'safe-export', 'name': 'Safe export'})
        service.add_step(workflow['key'], {'key': 'input', 'kind': 'input', 'name': 'Question'})
        service.run(workflow['key'], {
            'api_key': 'secret-api', 'password': 'secret-password', 'cookie': 'secret-cookie',
            'token': 'secret-token', 'secret': 'secret-value', 'file_content': 'raw document body',
        })
        exported = json.dumps(service.export_config(workflow['key']))
        for forbidden in ('secret-api', 'secret-password', 'secret-cookie', 'secret-token',
                          'secret-value', 'raw document body', 'run_context'):
            self.assertNotIn(forbidden, exported)

    def make_flow(self, key='run', policy='pause'):
        service = workflows.WorkflowService()
        service.create({'key': key, 'name': key})
        service.add_step(key, {'key': 'first', 'kind': 'agent', 'agent_key': 'agent-a'})
        service.add_step(key, {'key': 'second', 'kind': 'agent', 'agent_key': 'agent-b', 'on_error': policy})
        return service

    def test_manual_retry_preserves_success_and_cancel_is_terminal(self):
        service = self.make_flow()
        with patch('app.workflows.service.agent_chat.answer', side_effect=[{'ok': True, 'text': 'one'}, {'ok': False, 'error': 'unavailable'}, {'ok': True, 'text': 'two'}]) as answer:
            failed = service.start_run('run', {'question': 'q'})
            self.assertEqual(failed['status'], 'error')
            result = service.retry_run(failed['id'])
        self.assertEqual(result['execution_counts'], {'first': 1, 'second': 2})
        self.assertEqual(answer.call_count, 3)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(service.get_run(result['id'])['status'], 'success')
        self.assertEqual(service.list_runs('run')[0]['id'], result['id'])
        with self.assertRaises(ValueError):
            service.retry_run(result['id'])
        service.create({'key': 'approval', 'name': 'Approval'})
        service.add_step('approval', {'key': 'review', 'kind': 'approval'})
        waiting = service.start_run('approval', {})
        self.assertEqual(service.cancel_run(waiting['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError):
            service.approve_run(waiting['id'], True)

    def test_retry_once_stops_after_two_failures_and_skip_continues(self):
        service = self.make_flow(policy='retry_once')
        with patch('app.workflows.service.agent_chat.answer', side_effect=[{'ok': True, 'text': 'one'}, RuntimeError('bad'), RuntimeError('bad')]) as answer:
            result = service.start_run('run', {})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(answer.call_count, 3)
        service.update_step('run', 'second', {'on_error': 'skip'})
        with patch('app.workflows.service.agent_chat.answer', return_value={'ok': False, 'error': 'bad'}):
            skipped = service.retry_run(result['id'])
        self.assertEqual(skipped['status'], 'success')
        self.assertEqual(skipped['steps'][1]['status'], 'skipped')

    def test_disabled_entries_and_invalid_decisions_rejected(self):
        service = self.make_flow()
        service.update('run', {'status': 'disabled'})
        with self.assertRaises(ValueError):
            service.start_run('run', {})
        service.update('run', {'status': 'draft'})
        with self.assertRaises(ValueError):
            service.start_run('run', {}, trigger_type='schedule')
        registry.set_enabled('agent', 'agent-a', False)
        with self.assertRaises(ValueError):
            service.start_run('run', {})
        registry.set_enabled('agent', 'agent-a', True)
        self.add_hub_entry('skill', 'my-skill')
        service.add_step('run', {'key': 'skill', 'kind': 'skill', 'agent_key': 'agent-a', 'skill_key': 'my-skill'})
        registry.set_enabled('skill', 'my-skill', False)
        with self.assertRaises(ValueError):
            service.start_run('run', {})
        service.create({'key': 'approval', 'name': 'Approval'})
        service.add_step('approval', {'key': 'review', 'kind': 'approval'})
        result = service.start_run('approval', {})
        for decision in ('yes', None, {}, 1):
            with self.assertRaises(ValueError):
                service.approve_run(result['id'], decision)
        self.assertEqual(service.get_run(result['id'])['status'], 'waiting_approval')
        self.assertEqual(service.approve_run(result['id'], 'rejected')['status'], 'cancelled')

    def test_input_mapping_skill_context_and_summary_safety(self):
        self.add_hub_entry('skill', 'my-skill')
        (self.skills / 'my-skill' / 'SKILL.md').write_text('Summarize evidence. api_key=sk-private-value')
        service = workflows.WorkflowService()
        service.create({'key': 'mapped', 'name': 'Mapped'})
        service.add_step('mapped', {'key': 'input', 'kind': 'input', 'config': {'fields': ['question']}})
        service.add_step('mapped', {'key': 'skill', 'kind': 'skill', 'agent_key': 'agent-a', 'skill_key': 'my-skill', 'input_map': {'question': 'input.question'}, 'config': {'tier': 'reasoning', 'scope_label': 'History', 'web_mode': 'off'}})
        output = {'ok': True, 'text': 'x' * 30000 + ' sk-private-value', 'hits': [{'id': 7, 'detail': 'raw document body', 'api_key': 'secret-hit'}] * 30, 'trace': [{'tool': 'web_search', 'query': 'secret-query', 'status': 'done'}], 'prompt': 'private prompt'}
        with patch('app.workflows.service.agent_chat.answer', return_value=output) as answer:
            result = service.start_run('mapped', {'question': 'topic', 'ignored': 'ignored-value', 'password': 'secret-input'})
        self.assertEqual(answer.call_args.args[0], 'topic')
        self.assertIn('Summarize evidence', answer.call_args.kwargs['extra_instructions'])
        self.assertNotIn('sk-private-value', answer.call_args.kwargs['extra_instructions'])
        self.assertEqual(answer.call_args.kwargs['tier'], 'reasoning')
        self.assertEqual(answer.call_args.kwargs['scope_label'], 'History')
        self.assertEqual(answer.call_args.kwargs['web_mode'], 'off')
        serialized = json.dumps(result)
        for forbidden in ('sk-private-value', 'secret-input', 'secret-hit', 'secret-query', 'raw document body', 'private prompt', 'ignored-value'):
            self.assertNotIn(forbidden, serialized)
        self.assertLessEqual(len(result['steps'][1]['output']['text']), 8000)
        self.assertLessEqual(len(result['steps'][1]['output']['hits']), 8)

    def test_file_paths_and_invalid_maps_schemas_rejected(self):
        service = workflows.WorkflowService()
        service.create({'key': 'files', 'name': 'Files'})
        service.add_step('files', {'key': 'input', 'kind': 'input', 'config': {'accept': 'file'}})
        for path in ('/etc/passwd', '../outside.txt'):
            with self.assertRaises(ValueError):
                service.start_run('files', {'file_path': path})
        outside = self.root.parent / 'outside-file'
        (self.root / 'link').symlink_to(outside)
        with self.assertRaises(ValueError):
            service.start_run('files', {'file_path': str(self.root / 'link')})
        safe = self.root / 'safe.txt'
        safe.write_text('raw file body')
        result = service.start_run('files', {'file_path': str(safe)})
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('raw file body', json.dumps(result))
        for body in ({'input_map': {'question': {'eval': 'code'}}}, {'output_schema': {'type': 'unknown'}}):
            with self.assertRaises(ValueError):
                service.add_step('files', {'key': 'bad', 'kind': 'agent', 'agent_key': 'agent-a', **body})

    def test_output_schema_failure_and_missing_map_do_not_call_agent(self):
        service = self.make_flow()
        service.update_step('run', 'first', {'input_map': {'question': 'input.missing'}})
        with patch('app.workflows.service.agent_chat.answer') as answer:
            result = service.start_run('run', {})
        self.assertEqual(result['status'], 'error')
        answer.assert_not_called()
        service.update_step('run', 'first', {'input_map': {}, 'output_schema': {'type': 'object', 'required': ['missing']}})
        with patch('app.workflows.service.agent_chat.answer', return_value={'ok': True, 'text': 'done'}):
            result = service.start_run('run', {})
        self.assertEqual(result['status'], 'error')

    def test_agent_extra_instructions_are_system_context_with_bounded_budget(self):
        def model(tier, messages, tools, executor, **kwargs):
            self.assertEqual(messages[-1], {'role': 'user', 'content': 'question'})
            self.assertIn('[workflow skill context]', messages[0]['content'])
            self.assertIn('Summarize evidence', messages[0]['content'])
            self.assertNotIn('sk-private-value', messages[0]['content'])
            self.assertEqual(kwargs['max_rounds'], 8)
            self.assertNotIn('shell', [t['function']['name'] for t in tools])
            return 'done', [], ''
        with patch.object(llm, 'chat_with_tools', side_effect=model):
            self.assertTrue(chat.answer('question', [], extra_instructions='Summarize evidence. api_key=sk-private-value')['ok'])

    def test_output_schema_projects_declared_agent_fields_only(self):
        service = workflows.WorkflowService()
        service.create({'key': 'projected', 'name': 'Projected'})
        service.add_step('projected', {'key': 'agent', 'kind': 'agent', 'agent_key': 'agent-a',
                                       'output_schema': {'type': 'object', 'additionalProperties': False,
                                                         'properties': {'text': {'type': 'string'}, 'score': {'type': 'integer'}},
                                                         'required': ['text', 'score']}})
        response = {'ok': True, 'text': 'summary', 'score': 3, 'prompt': 'private',
                    'api_key': 'sk-secret', 'hits': [{'detail': 'raw'}]}
        with patch('app.workflows.service.agent_chat.answer', return_value=response):
            result = service.start_run('projected', {'question': 'q'})
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['steps'][0]['output'], {'text': 'summary', 'score': 3})
        self.assertNotIn('sk-secret', json.dumps(result))

    def test_file_input_materializes_bounded_text_for_agent(self):
        service = workflows.WorkflowService()
        service.create({'key': 'material', 'name': 'Material'})
        service.add_step('material', {'key': 'input', 'kind': 'input', 'config': {'fields': ['file_path'], 'accept': 'file'}})
        service.add_step('material', {'key': 'agent', 'kind': 'agent', 'agent_key': 'agent-a',
                                      'input_map': {'question': 'input.file_path'}})
        safe = self.root / 'material.md'
        safe.write_text('# Topic\nThis is indexed course material.', encoding='utf-8')
        seen = {}
        def answer(question, hits, **kwargs):
            seen['question'] = question
            return {'ok': True, 'text': 'done'}
        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            result = service.start_run('material', {'file_path': str(safe)})
        self.assertEqual(result['status'], 'success')
        self.assertIn('This is indexed course material.', seen['question'])
        self.assertNotEqual(seen['question'].strip(), str(safe))
        self.assertNotIn('This is indexed course material.', json.dumps(result))

    def test_filename_input_materializes_material_context(self):
        service = workflows.WorkflowService()
        service.create({'key': 'filename-material', 'name': 'Filename material'})
        service.add_step('filename-material', {'key': 'input', 'kind': 'input',
                                               'config': {'fields': ['filename'], 'accept': 'file'}})
        service.add_step('filename-material', {'key': 'agent', 'kind': 'agent', 'agent_key': 'agent-a',
                                               'input_map': {'question': 'input.filename'}})
        material = self.root / 'filename.md'
        material.write_text('Filename material body.', encoding='utf-8')
        seen = {}
        def answer(question, hits, **kwargs):
            seen['question'] = question
            return {'ok': True, 'text': 'done'}
        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            result = service.start_run('filename-material', {'filename': str(material)})
        self.assertEqual(result['status'], 'success')
        self.assertIn('Filename material body.', seen['question'])
        self.assertNotIn(str(material), seen['question'])

    def test_agent_payload_filename_is_plain_text_and_not_materialized(self):
        service = workflows.WorkflowService()
        service.create({'key': 'path-flow', 'name': 'Path flow'})
        service.add_step('path-flow', {'key': 'input', 'kind': 'input', 'config': {'fields': ['file_path'], 'accept': 'file'}})
        service.add_step('path-flow', {'key': 'producer', 'kind': 'agent', 'agent_key': 'agent-a',
                                       'output_schema': {'type': 'object', 'properties': {'payload': {'type': 'string'}},
                                                         'required': ['payload']}})
        service.add_step('path-flow', {'key': 'consumer', 'kind': 'agent', 'agent_key': 'agent-b',
                                       'input_map': {'question': 'producer.payload'}})
        initial = self.root / 'initial.md'
        generated = self.root / 'generated.md'
        initial.write_text('initial', encoding='utf-8')
        generated.write_text('must not be read', encoding='utf-8')
        calls = []
        def answer(question, hits, **kwargs):
            calls.append(question)
            if len(calls) == 1:
                return {'ok': True, 'payload': generated.name}
            return {'ok': True, 'text': 'unexpected'}
        with patch('app.workflows.service.agent_chat.answer', side_effect=answer):
            result = service.start_run('path-flow', {'file_path': str(initial)})
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], generated.name)
        self.assertNotIn('must not be read', calls[1])

    def test_output_schema_top_level_must_be_object(self):
        service = workflows.WorkflowService()
        service.create({'key': 'schema-kind', 'name': 'Schema kind'})
        for schema in ({'type': 'string'}, {'type': 'array'}, {'type': 'boolean'}):
            with self.assertRaisesRegex(ValueError, '顶层 output_schema 必须是 object'):
                service.add_step('schema-kind', {'key': 'step' + schema['type'], 'kind': 'agent',
                                                 'agent_key': 'agent-a', 'output_schema': schema})

    def test_skip_error_is_retained_after_later_success(self):
        service = workflows.WorkflowService()
        service.create({'key': 'partial', 'name': 'Partial'})
        service.add_step('partial', {'key': 'skipped', 'kind': 'agent', 'agent_key': 'agent-a', 'on_error': 'skip'})
        service.add_step('partial', {'key': 'success', 'kind': 'agent', 'agent_key': 'agent-b'})
        with patch('app.workflows.service.agent_chat.answer', side_effect=[RuntimeError('first failed'), {'ok': True, 'text': 'done'}]):
            result = service.start_run('partial', {'question': 'q'})
        self.assertEqual(result['status'], 'success')
        self.assertIn('first failed', result['error'])
        self.assertEqual([step['status'] for step in result['steps']], ['skipped', 'success'])

    def test_builtin_workflow_skills_are_real_templates_and_secret_metadata_is_redacted(self):
        for key in ('material-recognition', 'wiki-extract', 'web-research', 'local-research'):
            directory = registry.TEMPLATE_SKILLS_DIR / key
            self.assertTrue((directory / 'manifest.json').is_file())
            self.assertTrue((directory / 'SKILL.md').is_file())
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'secret-meta', 'name': 'Visible', 'description': 'secret-value'})
        exported = json.dumps(service.export_config(workflow['key']), ensure_ascii=False)
        self.assertNotIn('secret-value', exported)

    def test_non_json_values_raise_value_error(self):
        service = workflows.WorkflowService()
        with self.assertRaises(ValueError):
            service.create({'key': 'bad-json', 'name': 'Bad', 'trigger': {'value': {1, 2}}})
        workflow = service.create({'key': 'bad-step-json', 'name': 'Bad step'})
        with self.assertRaises(ValueError):
            service.add_step(workflow['key'], {'key': 'input', 'kind': 'input', 'config': {'value': {1, 2}}})

    def test_key_lookup_and_unique_conflicts_are_explicit(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': '123', 'name': 'Numeric key'})
        self.assertEqual(service.get('123')['key'], '123')
        with self.assertRaises(ValueError):
            service.get(workflow['id'])
        with self.assertRaises(ValueError):
            service.create({'key': '123', 'name': 'Duplicate'})
        service.add_step('123', {'key': 'same', 'kind': 'input'})
        with self.assertRaises(ValueError):
            service.add_step('123', {'key': 'same', 'kind': 'approval'})

    def test_nested_credentials_are_redacted_and_deleted_hub_files_rejected(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'nested-safe', 'name': 'Safe'})
        service.add_step('nested-safe', {'key': 'input', 'kind': 'input', 'config': {
            'nested': {'Authorization': 'Bearer top-secret', 'value': 'sk-live-secret'},
            'items': [{'password': 'pw-secret'}, {'ok': 'visible'}],
        }})
        exported = json.dumps(service.export_config('nested-safe'), ensure_ascii=False)
        for secret in ('top-secret', 'sk-live-secret', 'pw-secret'):
            self.assertNotIn(secret, exported)
        skill = self.skills / 'deleted-skill'
        skill.mkdir()
        (skill / 'SKILL.md').write_text('# skill', encoding='utf-8')
        (skill / 'manifest.json').write_text(json.dumps({'key': 'deleted-skill', 'name': 'Deleted', 'version': '1'}), encoding='utf-8')
        registry.sync()
        (skill / 'SKILL.md').unlink()
        with self.assertRaises(ValueError):
            service.add_step('nested-safe', {'key': 'skill-step', 'kind': 'skill', 'skill_key': 'deleted-skill', 'agent_key': 'laoba'})

    def test_reorder_rejects_id_strings_and_template_type_is_validated(self):
        service = workflows.WorkflowService()
        workflow = service.create({'key': 'order-types', 'name': 'Order types'})
        service.add_step('order-types', {'key': 'one', 'kind': 'input'})
        with self.assertRaises(ValueError):
            service.reorder_steps('order-types', ['1'])
        with self.assertRaises(ValueError):
            service.create_from_template([])


if __name__ == '__main__':
    unittest.main()
