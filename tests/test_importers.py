import json
from io import BytesIO
from tarfile import TarInfo, open as open_tar
from zipfile import ZipFile

from ai_memory.importers import (
  iter_export,
  iter_json_export,
  iter_shell_history,
)
from ai_memory.security import redact_secrets


def test_chatgpt_export_parser(tmp_path):
    path = tmp_path / "chatgpt.json"
    path.write_text(json.dumps([{"id": "conv1", "title": "Plans", "mapping": {
        "m1": {"message": {"author": {"role": "user"}, "content": {"parts": ["I live in Seattle"]}, "create_time": 1700000000}},
        "m2": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["Okay"]}}}
    }}]))
    rows = list(iter_json_export(path, "chatgpt"))
    assert len(rows) == 1 and rows[0]["source_record_id"] == "conv1:m1"
    assert rows[0]["text"] == "I live in Seattle" and rows[0]["timestamp"] is not None


def test_claude_and_gemini_shapes(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps([{"uuid": "claude1", "chat_messages": [{"uuid": "m", "sender": "human", "content": [{"type": "text", "text": "I like jazz"}]}]},
        {"id": "gemini1", "turns": [{"role": "user", "text": "I work in design"}]}]))
    assert [x["text"] for x in iter_json_export(path, "claude")] == ["I like jazz"]
    assert [x["text"] for x in iter_json_export(path, "gemini")] == ["I work in design"]


def test_gemini_takeout_activity_and_shell_formats(tmp_path):
    path = tmp_path / "gemini.json"
    path.write_text(json.dumps([{"title": "Prompted Gemini: What is a good hiking trail?", "time": "2025-01-02T03:04:05Z"}]))
    row = list(iter_json_export(path, "gemini"))[0]
    assert row["text"] == "What is a good hiking trail?" and row["timestamp"] is not None
    history = tmp_path / ".zsh_history"
    history.write_text(": 1735787045:0;git status\n#1735787046\npython tool.py\n")
    rows = list(iter_shell_history(history, "zsh"))
    assert [r["text"] for r in rows] == ["git status", "python tool.py"]
    assert rows[0]["timestamp"] is not None and rows[1]["timestamp"] is not None
    assert rows[0]["source_record_id"] != rows[1]["source_record_id"]


def test_secrets_are_removed():
    value = redact_secrets('password=hunter2 password=\'two secret words\' {"api_key":"json-secret-value"} --token cli-secret-value Bearer abcdefghijklmnop sk-12345678901234567890')
    assert "hunter2" not in value and "two secret words" not in value and "json-secret-value" not in value and "cli-secret-value" not in value
    assert "abcdefghijklmnop" not in value and "sk-12345678901234567890" not in value


def test_gemini_html_export_uses_stable_record_ids(tmp_path):
  path = tmp_path / 'MyActivity.html'
  path.write_text('''
    <div class="outer-cell">
      <div>Prompted I use OpenWrt.</div>
      <div>May 21, 2025, 3:51:34 PM EDT</div>
      <a href="https://gemini.google.com/app/abc">Open</a>
    </div>
    <div class="outer-cell">
      <div>Gemini replied with an instruction.</div>
    </div>
  ''')
  first = list(iter_export(path, 'gemini'))
  second = list(iter_export(path, 'gemini'))
  assert len(first) == 1
  assert first == second
  assert first[0]['text'] == 'I use OpenWrt.'
  assert first[0]['metadata']['url'].endswith('/abc')


def test_chatgpt_zip_export_reads_nested_conversations(tmp_path):
  path = tmp_path / 'export.zip'
  chat = [{
    'id': 'chat-1',
    'mapping': {
      'message-1': {
        'message': {
          'author': {'role': 'user'},
          'content': {'parts': ['I use Docker.']},
        },
      },
    },
  }]
  with ZipFile(path, 'w') as archive:
    archive.writestr('export/conversations.json', json.dumps(chat))
  rows = list(iter_export(path, 'chatgpt'))
  assert len(rows) == 1
  assert rows[0]['source_record_id'] == 'chat-1:message-1'


def test_export_rejects_ambiguous_zip_member(tmp_path):
  path = tmp_path / 'export.zip'
  with ZipFile(path, 'w') as archive:
    archive.writestr('a/conversations.json', '[]')
    archive.writestr('b/conversations.json', '[]')
  try:
    list(iter_export(path, 'chatgpt'))
  except ValueError as error:
    assert 'Expected one conversations.json' in str(error)
  else:
    raise AssertionError('Ambiguous archive was accepted')


def test_gemini_tgz_selects_gemini_activity(tmp_path):
  path = tmp_path / 'takeout.tgz'
  data = b'''
    <div class="outer-cell">
      <div>Prompted I use Qdrant.</div>
      <div>Oct 10, 2026, 10:00:00 AM EDT</div>
    </div>
  '''
  with open_tar(path, 'w:gz') as archive:
    for name, body in [
      ('Takeout/My Activity/Search/MyActivity.html', b'noise'),
      ('Takeout/My Activity/Gemini Apps/MyActivity.html', data),
    ]:
      info = TarInfo(name)
      info.size = len(body)
      archive.addfile(info, BytesIO(body))
  rows = list(iter_export(path, 'gemini'))
  assert len(rows) == 1
  assert rows[0]['text'] == 'I use Qdrant.'
