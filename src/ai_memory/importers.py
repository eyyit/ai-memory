from __future__ import annotations

import json
import re
import hashlib
import subprocess
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from tarfile import open as open_tar
from typing import Iterator
from zipfile import ZipFile


def _dt(value):
    if value is None: return None
    try:
        if isinstance(value, (int, float)): return datetime.fromtimestamp(value, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError): return None


def _text(value) -> str:
    if isinstance(value, str): return value
    if isinstance(value, list): return "\n".join(x for part in value if (x := _text(part)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), str): return value["text"]
        if isinstance(value.get("content"), (str, list)): return _text(value["content"])
        return ""
    return ""


def _human_role(value) -> bool:
    return str(value or "").lower() in {"user", "human", "you", "prompt"}


def _bounded_identity(value: str, limit: int = 200) -> str:
    if len(value) <= limit: return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return value[:limit - 21] + "-" + suffix


def _messages(conversation, source: str) -> list[tuple[str, str, datetime | None]]:
    """Normalize common ChatGPT, Claude, and Gemini JSON export shapes."""
    result = []
    mapping = conversation.get("mapping") if isinstance(conversation, dict) else None
    if source == "chatgpt" and isinstance(mapping, dict):  # ChatGPT conversations.json tree
        rows = []
        for mid, node in mapping.items():
            msg = (node or {}).get("message")
            if not msg: continue
            author = (msg.get("author") or {}).get("role")
            if not _human_role(author): continue
            content = _text((msg.get("content") or {}).get("parts", []))
            if content.strip(): rows.append((str(mid), content, _dt(msg.get("create_time"))))
        return sorted(rows, key=lambda x: x[2] or datetime.min.replace(tzinfo=timezone.utc))

    if source == "claude": messages = conversation.get("chat_messages") or conversation.get("messages") or []
    elif source == "gemini": messages = conversation.get("turns") or conversation.get("messages") or []
    else: messages = conversation.get("messages") or conversation.get("chat_messages") or []
    if isinstance(messages, dict): messages = list(messages.values())
    if not isinstance(messages, list): return result
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict): continue
        author = msg.get("role") or msg.get("sender") or msg.get("author")
        if isinstance(author, dict): author = author.get("role") or author.get("name")
        if not _human_role(author): continue
        body = _text(msg.get("content") or msg.get("text") or msg.get("message"))
        if body.strip(): result.append((str(msg.get("uuid") or msg.get("id") or msg.get("message_id") or i), body, _dt(msg.get("created_at") or msg.get("timestamp") or msg.get("create_time"))))
    # Gemini Takeout may represent turns in a list of {role, text} objects.
    return result


def _iter_json_data(data, source: str) -> Iterator[dict]:
    if isinstance(data, dict):
        conversations = data.get("conversations") or data.get("chats") or data.get("items")
        if conversations is None: conversations = [data]
    else: conversations = data
    if not isinstance(conversations, list): raise ValueError("Expected an array of conversation records")
    for ci, conv in enumerate(conversations):
        if not isinstance(conv, dict): continue
        stable = conv.get("id") or conv.get("uuid") or conv.get("conversation_id")
        if not stable and conv.get("title"):
            stable = "title-" + hashlib.sha256(str(conv["title"]).encode("utf-8")).hexdigest()[:20]
        cid = _bounded_identity(str(stable or f"conversation-{ci}"))
        messages = _messages(conv, source)
        if source == "gemini" and not messages and isinstance(conv.get("title"), str) and re.match(r"(?i)^(prompted|asked)\s", conv["title"]):
            # Google Takeout's Gemini activity JSON stores prompts as activity titles.
            body = re.sub(r"(?i)^(?:prompted|asked)\s+(?:gemini(?: apps)?\s*[:\-]?\s*)?", "", conv["title"]).strip()
            stamp = _dt(conv.get("time") or conv.get("timestamp") or conv.get("timeStarted"))
            if body:
                seed = f"{conv.get('id', '')}\0{stamp}\0{body}"
                messages = [(str(conv.get("id") or "activity-" + hashlib.sha256(seed.encode()).hexdigest()[:20]), body, stamp)]
        for mid, body, stamp in messages:
            yield {"source": source, "source_record_id": _bounded_identity(f"{cid}:{mid}", 500), "conversation_id": cid,
                "timestamp": stamp, "text": body, "metadata": {"title": conv.get("title", "")}}


def iter_json_export(path: str | Path, source: str) -> Iterator[dict]:
  data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
  yield from _iter_json_data(data, source)


class _GeminiActivityParser(HTMLParser):
  def __init__(self):
    super().__init__()
    self.depth = 0
    self.active = None
    self.records = []
    self.skip = 0

  def handle_starttag(self, tag, attrs):
    values = dict(attrs)
    if tag == 'div':
      self.depth += 1
      if 'outer-cell' in values.get('class', '').split():
        self.active = {'depth': self.depth, 'text': [], 'links': []}
    if tag in {'script', 'style'}:
      self.skip += 1
    if self.active and tag == 'a':
      link = values.get('href', '')
      if link.startswith('https://gemini.google.com/app/'):
        self.active['links'].append(link)

  def handle_endtag(self, tag):
    if tag in {'script', 'style'}:
      self.skip = max(0, self.skip - 1)
    if tag == 'div':
      if self.active and self.depth == self.active['depth']:
        self.records.append(self.active)
        self.active = None
      self.depth -= 1

  def handle_data(self, data):
    if self.active and not self.skip and data.strip():
      self.active['text'].append(data.strip())


def _iter_gemini_html(text: str) -> Iterator[dict]:
  parser = _GeminiActivityParser()
  parser.feed(text)
  for record in parser.records:
    parts = record['text']
    prompted = next(
      (item for item in parts if item.startswith('Prompted')),
      None,
    )
    if prompted is None:
      continue
    body = prompted[len('Prompted'):].strip()
    if not body:
      continue
    date = next(
      (item for item in parts if re.match(
        r'^[A-Z][a-z]{2} \d{1,2}, \d{4},', item,
      )),
      '',
    )
    link = next(iter(record['links']), '')
    seed = '\0'.join((link, date, body))
    identity = hashlib.sha256(seed.encode('utf-8')).hexdigest()
    yield {
      'source': 'gemini',
      'source_record_id': f'gemini-html:{identity}',
      'conversation_id': link or f'gemini-html:{identity[:24]}',
      'timestamp': None,
      'text': body,
      'metadata': {'activity_date': date, 'url': link},
    }


def _select_archive_member(members, source):
  leaf = 'MyActivity.html' if source == 'gemini' else 'conversations.json'
  matches = [
    item for item in members
    if not item.isdir() and Path(item.name).name == leaf
  ]
  if source == 'gemini' and len(matches) > 1:
    gemini = [
      item for item in matches
      if 'gemini' in item.name.lower().split('/')
      or 'gemini apps' in item.name.lower().split('/')
    ]
    matches = gemini
  if len(matches) != 1:
    raise ValueError(f'Expected one {leaf} in archive')
  member = matches[0]
  if member.size > 1_000_000_000:
    raise ValueError(f'{leaf} exceeds the 1 GB import limit')
  return member


def iter_export(path: str | Path, source: str) -> Iterator[dict]:
  path = Path(path)
  suffix = path.suffix.lower()
  if suffix in {'.html', '.htm'}:
    if source != 'gemini':
      raise ValueError('HTML activity imports require source gemini')
    yield from _iter_gemini_html(path.read_text(encoding='utf-8-sig'))
    return
  if suffix == '.zip':
    with ZipFile(path) as archive:
      class Entry:
        def __init__(self, info):
          self.name = info.filename
          self.size = info.file_size

        def isdir(self):
          return self.name.endswith('/')

      member = _select_archive_member(
        [Entry(info) for info in archive.infolist()],
        source,
      )
      with archive.open(member.name) as stream:
        raw = stream.read().decode('utf-8-sig')
      if source == 'gemini':
        yield from _iter_gemini_html(raw)
      else:
        yield from _iter_json_data(json.loads(raw), source)
    return
  if suffix == '.tgz' or path.name.lower().endswith('.tar.gz'):
    with open_tar(path, 'r:gz') as archive:
      member = _select_archive_member(archive.getmembers(), source)
      if not member.isfile():
        raise ValueError('Expected a regular archive member')
      stream = archive.extractfile(member)
      if stream is None:
        raise ValueError('Could not read archive member')
      with stream:
        raw = stream.read().decode('utf-8-sig')
      if source == 'gemini':
        yield from _iter_gemini_html(raw)
      else:
        yield from _iter_json_data(json.loads(raw), source)
    return
  yield from iter_json_export(path, source)


def iter_shell_history(path: str | Path, source: str | None = None) -> Iterator[dict]:
    source = source or Path(path).suffix.lstrip(".") or "shell-history"
    path_key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:12]
    pending_timestamp = None
    for i, line in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        value = line.strip()
        if re.fullmatch(r"#\d{9,}", value):
            pending_timestamp = _dt(int(value[1:])); continue
        zsh = re.match(r"^: (\d{9,}):\d+;(.*)$", value)
        if zsh:
            pending_timestamp, value = _dt(int(zsh.group(1))), zsh.group(2).strip()
        if value:
            yield {"source": source, "source_record_id": f"{path_key}:line-{i}", "timestamp": pending_timestamp, "text": value,
                "metadata": {"line": i, "import_kind": "shell-history"}}
            pending_timestamp = None


def iter_git_history(repository: str | Path, max_commits: int = 10000) -> Iterator[dict]:
    # Commit metadata is an intentionally bounded, user-invoked import; no working files or patches are read.
    repo_path = str(Path(repository).resolve())
    repo_key = hashlib.sha256(repo_path.encode()).hexdigest()[:12]
    output = subprocess.run(["git", "-c", f"safe.directory={repo_path}", "-C", repo_path, "log", f"-n{max_commits}", "--format=%H%x00%aI%x00%s%x00%b%x00"],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    chunks = output.split("\x00")
    for i in range(0, len(chunks) - 3, 4):
        commit, stamp, subject, body = chunks[i:i + 4]
        commit = commit.strip()
        if commit:
            message = (subject + ("\n" + body if body.strip() else "")).strip()
            yield {"source": "git", "source_record_id": f"{repo_key}:{commit}", "conversation_id": str(repository),
                "timestamp": _dt(stamp), "text": message, "metadata": {"commit": commit, "repository": Path(repository).name}}


def import_records(service, instance_id: str, records) -> dict:
    count = duplicates = memories = pending = 0
    for row in records:
        result = service.ingest(instance_id=instance_id, **row)
        count += 1
        pending += int(result.get("pending", False))
        duplicates += int(result["duplicate"] and not result.get("pending", False))
        memories += result["memories_created"]
    return {"records": count, "duplicates": duplicates, "pending": pending, "memories_created": memories}
