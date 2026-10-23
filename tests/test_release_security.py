"""Bounded, offline regression checks for release security boundaries."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from fastapi import UploadFile, HTTPException
import tests  # Establish synthetic-only storage before application imports.
from services.resource_limits import ParserLimitError, validate_zip, validate_pdf


def archive(entries, compression=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=compression) as z:
        for name, data in entries:
            z.writestr(name, data)
    return out.getvalue()


class ResourceBudgetTests(unittest.TestCase):
    def test_zip_member_count_and_normal_document(self):
        validate_zip(archive([('lesson.txt', b'synthetic lesson')]))
        with self.assertRaises(ParserLimitError):
            validate_zip(archive([(str(i), b'x') for i in range(101)]))

    def test_zip_member_total_and_expansion_limits(self):
        for raw in (archive([('large', b'x' * (10 * 1024**2 + 1))]),
                    archive([(str(i), b'x' * (9 * 1024**2)) for i in range(6)]),
                    archive([('compressed', b'x' * 100_000)], zipfile.ZIP_DEFLATED)):
            with self.subTest(size=len(raw)), self.assertRaises(ParserLimitError):
                validate_zip(raw)

    def test_pdf_pages_and_raster_size_before_processing(self):
        import pymupdf
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), 'Synthetic PDF')
            validate_pdf(doc)
            for _ in range(50):
                doc.new_page()
            with self.assertRaises(ParserLimitError):
                validate_pdf(doc)
        with pymupdf.open() as doc:
            doc.new_page(width=2000, height=2000)
            with self.assertRaises(ParserLimitError):
                validate_pdf(doc)

    def test_deleted_records_do_not_reappear_from_legacy_data_or_upload(self):
        from scripts.reindex_all import load_catalog_data, load_inline_documents, load_upload_documents
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            legacy = root / 'files_data.json'
            active = root / 'conversations_data.json'
            legacy.write_text(json.dumps({'files': [{'id': 'deleted', 'name': 'gone.txt'}],
                                          'file_contents': {'deleted': 'private synthetic text'}}))
            active.write_text(json.dumps({'deleted_document_ids': ['deleted']}))
            metadata, contents = load_catalog_data(legacy, active)
            self.assertEqual(contents, {})
            self.assertEqual(metadata['deleted']['review_status'], 'deleted')
            self.assertEqual(load_inline_documents(legacy, conversations_path=active)[0], [])
            uploads = root / 'uploads'; uploads.mkdir()
            filename = '7e6525c4-5e24-4f71-aea3-d049347c0028_gone.txt'
            (uploads / filename).write_text('residual original')
            with patch('scripts.reindex_all.stable_upload_id', return_value='deleted'):
                docs, _, _ = load_upload_documents(uploads, use_cache=False, metadata=metadata,
                    extractor=lambda _: self.fail('Deleted original was parsed'))
            self.assertEqual(docs, [])

    def test_login_limits_bound_unknown_account_attempts(self):
        from services import login_limits
        with patch.object(login_limits, '_attempts', __import__('collections').OrderedDict()):
            for _ in range(10):
                login_limits.admit('synthetic-account', '127.0.0.1')
            with self.assertRaises(HTTPException) as error:
                login_limits.admit('synthetic-account', '127.0.0.1')
            self.assertEqual(error.exception.status_code, 429)

    def test_optional_agent_and_preview_share_upload_budgets(self):
        from services.file_agent.skills import FileSkills, FileSkillError
        import pymupdf
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            office = root / 'over.docx'
            office.write_bytes(archive([(str(i), b'x') for i in range(101)]))
            pdf = root / 'over.pdf'
            with pymupdf.open() as doc:
                for _ in range(51): doc.new_page()
                doc.save(pdf)
            skills = FileSkills({'office': office, 'pdf': pdf}, root / 'out')
            for identifier in ('office', 'pdf'):
                with self.subTest(identifier=identifier), self.assertRaises(FileSkillError):
                    skills.inspect_file(identifier)

    def test_many_line_index_is_rejected_before_vector_allocation(self):
        from services.knowledge.vector_store import VectorStore
        from unittest.mock import Mock
        store = object.__new__(VectorStore)
        store._embedder, store._collection = Mock(), Mock()
        # Only 8 KB, sufficient to exercise the original expansion boundary.
        with self.assertRaises(ParserLimitError):
            store.index_document({'id': 'synthetic', 'name': 'fixture.txt'}, ('A' * 16 + '\n') * 513)
        store._embedder.embed.assert_not_called()
        store._collection.upsert.assert_not_called()

    def test_index_vectors_are_batched_and_bulk_is_per_document(self):
        from services.knowledge.vector_store import VectorStore
        from unittest.mock import Mock
        store = object.__new__(VectorStore)
        store._index_revision = 0
        store._collection = Mock()
        store._embedder = Mock(name='fixture')
        store._embedder.embed.side_effect = lambda texts: [[0., 1.] for _ in texts]
        text = ('A' * 16 + '\n') * 100
        self.assertEqual(store.bulk_index_documents([({'id': 'one'}, text), ({'id': 'two'}, text)]), 200)
        self.assertTrue(all(len(c.args[0]) <= 32 for c in store._embedder.embed.call_args_list))
        self.assertEqual(sum(len(c.kwargs['ids']) for c in store._collection.upsert.call_args_list), 200)


class SessionRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_admission_survives_repeated_cancellation_until_thread_stops(self):
        import threading
        from services.resource_limits import await_thread_completion
        entered, finish = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        slot = asyncio.Semaphore(1)
        def worker():
            loop.call_soon_threadsafe(entered.set)
            if not finish.wait(5):
                raise AssertionError('Test did not release the controlled worker')
        async def protected():
            async with slot:
                await await_thread_completion(worker)
        task = asyncio.create_task(protected())
        await entered.wait()
        try:
            for _ in range(2):
                task.cancel(); await asyncio.sleep(0)
                self.assertTrue(slot.locked())
        finally:
            finish.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(slot.locked())

    async def test_logout_remains_revoked_after_an_older_request_completes(self):
        import main
        import httpx
        from tests.account_fixtures import fixture_accounts
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch.object(main, 'CONVERSATIONS_FILE', str(root / 'conversations.json')), \
                 patch.object(main, 'SESSIONS_FILE', str(root / 'sessions.json')), \
                 patch.dict(main.VALID_ACCOUNTS, fixture_accounts()):
                store = main.DataStore()
                from datetime import datetime
                initial = {'account': 'student-test', 'created_at': datetime.now().isoformat()}
                await store.set_session('revocation-fixture', initial, create=True)
                entered, resume = asyncio.Event(), asyncio.Event()
                async def older_request():
                    captured = await store.get_session('revocation-fixture')
                    entered.set(); await resume.wait()
                    await store.set_session('revocation-fixture', captured)
                task = asyncio.create_task(older_request()); await entered.wait()
                with patch.object(main, 'data_store', store):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://fixture') as client:
                        headers = {'X-Session-ID': 'revocation-fixture'}
                        self.assertEqual((await client.post('/api/logout', headers=headers)).status_code, 200)
                        resume.set(); await task
                        self.assertEqual((await client.get('/api/session', headers=headers)).status_code, 401)
                self.assertIsNone(await store.get_session('revocation-fixture'))
                await store.set_session('new-fixture', initial, create=True)
                await store.set_session('new-fixture', {**initial, 'last_activity': 'updated'})
                self.assertEqual((await store.get_session('new-fixture'))['last_activity'], 'updated')
                await asyncio.sleep(0)  # Finish local persistence before fixture removal.


class IsolatedParserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from services import parser_runtime
        self.runtime = parser_runtime
        self.temp = tempfile.TemporaryDirectory(prefix='hk_parser_security_')
        self.root = Path(self.temp.name)
        self.slot = asyncio.Semaphore(1)
        self.patches = [patch.object(parser_runtime, 'DATA_DIR', str(self.root)),
                        patch.object(parser_runtime, 'PROCESSING_SLOT', self.slot)]
        for item in self.patches: item.start()
        self.children = []

    async def asyncTearDown(self):
        for child in self.children:
            if child.returncode is None: await self.runtime.stop_worker(child)
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def source(self, raw=b'synthetic parser text', name='lesson.txt'):
        return UploadFile(filename=name, file=io.BytesIO(raw))

    async def test_real_worker_text_pdf_and_truncation(self):
        result = await self.runtime.parse_upload_isolated(self.source())
        self.assertEqual(result['content'], 'synthetic parser text')
        self.assertFalse(result['is_truncated'])
        result = await self.runtime.parse_upload_isolated(self.source(b'x' * 1_000_001))
        self.assertEqual(len(result['content']), 1_000_000)
        self.assertTrue(result['is_truncated'])
        import pymupdf
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), 'Synthetic offline PDF')
            raw = doc.tobytes()
        result = await self.runtime.parse_upload_isolated(self.source(raw, 'lesson.pdf'))
        self.assertIn('Synthetic offline PDF', result['content'])
        self.assertEqual(list((self.root / '.parser_tasks').iterdir()), [])
        self.assertFalse(self.slot.locked())

    async def test_global_gate_rejects_busy_without_creating_task_files(self):
        await self.slot.acquire()
        try:
            with self.assertRaises(HTTPException) as error:
                await self.runtime.parse_upload_isolated(self.source())
            self.assertEqual(error.exception.status_code, 429)
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            self.slot.release()

    async def exercise_interrupt(self, cancel):
        original = asyncio.create_subprocess_exec
        ready = asyncio.Event()
        async def controlled_child(*args, **kwargs):
            if args[0] != sys.executable:
                return await original(*args, **kwargs)
            child = await original(sys.executable, '-c', 'import time; time.sleep(30)', **kwargs)
            self.children.append(child)
            ready.set()
            return child
        with patch.object(self.runtime.asyncio, 'create_subprocess_exec', controlled_child), \
             patch.object(self.runtime, 'PARSER_TIMEOUT', 30 if cancel else .2):
            task = asyncio.create_task(self.runtime.parse_upload_isolated(self.source()))
            await asyncio.wait_for(ready.wait(), 10)
            if cancel: task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancel else ParserLimitError):
                await task
        self.assertTrue(all(item.returncode is not None for item in self.children))
        self.assertFalse(self.slot.locked())
        self.assertEqual(list((self.root / '.parser_tasks').iterdir()), [])

    async def test_timeout_reaps_process_and_cleans_temporary_files(self):
        await self.exercise_interrupt(False)

    async def test_client_cancellation_reaps_process_and_releases_admission(self):
        await self.exercise_interrupt(True)


if __name__ == '__main__':
    unittest.main()
