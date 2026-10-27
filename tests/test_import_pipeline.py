import json
import shutil
import subprocess

import pytest

from ai_memory.importers import (
  import_records,
  iter_export,
  iter_git_history,
  iter_json_export,
  iter_shell_history,
)


def test_export_reimport_extracts_only_new_records(rig, tmp_path):
    service, _, extractor, _, _ = rig
    extractor.results = [{"content": "Prefers Python"}]
    path = tmp_path / "export.json"
    messages = [{"id": "1", "role": "user", "text": "I prefer Python. password=private-value"}]
    path.write_text(json.dumps([{"id": "conversation", "messages": messages}]))
    assert import_records(service, "home", iter_json_export(path, "chatgpt"))["records"] == 1
    service.process_extractions(instance_id="home")
    messages.append({"id": "2", "role": "user", "text": "I prefer Python"})
    path.write_text(json.dumps([{"id": "conversation", "messages": messages}]))
    result = import_records(service, "home", iter_json_export(path, "chatgpt"))
    assert result["duplicates"] == 1 and result["memories_created"] == 0
    service.process_extractions(instance_id="home")
    assert len(extractor.inputs) == 2
    assert all("private-value" not in value for value in extractor.inputs)
    assert len(service.get("home", service.list("home")[0]["id"])["sources"]) == 2


def test_appended_shell_history_has_one_new_extraction(rig, tmp_path):
    service, _, extractor, _, _ = rig
    extractor.results = []
    history = tmp_path / "history"
    history.write_text("git status\n")
    import_records(service, "home", iter_shell_history(history))
    service.process_extractions(instance_id="home")
    history.write_text("git status\nexport API_KEY=private-value\n")
    result = import_records(service, "home", iter_shell_history(history))
    assert result["records"] == 2 and result["duplicates"] == 1
    service.process_extractions(instance_id="home")
    assert len(extractor.inputs) == 2 and "private-value" not in extractor.inputs[-1]


def test_gemini_html_reimport_is_idempotent_and_redacted(rig, tmp_path):
  service, _, extractor, _, _ = rig
  extractor.results = []
  path = tmp_path / 'MyActivity.html'
  path.write_text('''
    <div class="outer-cell">
      <div>Prompted I use Docker. password=private-value</div>
      <div>Oct 10, 2026, 10:00:00 AM EDT</div>
    </div>
  ''')
  first = import_records(service, 'home', iter_export(path, 'gemini'))
  assert first['records'] == 1
  service.process_extractions(instance_id='home')
  again = import_records(service, 'home', iter_export(path, 'gemini'))
  assert again['duplicates'] == 1
  assert len(extractor.inputs) == 1
  assert 'private-value' not in extractor.inputs[0]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git unavailable")
def test_real_git_metadata_import_and_retry(rig, tmp_path):
    service, _, extractor, _, _ = rig
    extractor.results = []
    repo = tmp_path / "repository"
    repo.mkdir()
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false",
                    "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "Add tool api_key=private-value"], check=True, capture_output=True)
    rows = list(iter_git_history(repo))
    assert len(rows) == 1 and rows[0]["timestamp"] is not None
    assert rows[0]["metadata"]["commit"] in rows[0]["source_record_id"]
    import_records(service, "home", rows)
    service.process_extractions(instance_id="home")
    assert import_records(service, "home", iter_git_history(repo))["duplicates"] == 1
    assert len(extractor.inputs) == 1 and "private-value" not in extractor.inputs[0]
