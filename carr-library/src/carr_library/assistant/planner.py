"""Deterministic guided commands; no claims of general-language understanding."""
import json
import re
from .models import Plan, Step, text


HELP = '''Enter one command per line (English or Ukrainian), or a versioned JSON plan.
Create library "History" / Створи бібліотеку "Історія"
Import "C:/Books/source.txt" / Імпортуй "C:/Books/source.txt"
Import folder "C:/Books" / Імпортуй папку "C:/Books"
Search "railway transport" / Знайди "залізничний транспорт"
List books / Покажи книги
List libraries / Покажи бібліотеки
List voices / Покажи голоси
Audio effects / Покажи аудіоефекти
Player status / Стан програвача (open Assistant playback from the current reader)
Inspect audio "C:/Audio/source.wav" / Перевір аудіо "C:/Audio/source.wav"
Search evidence "railway" / Знайди докази "залізниця"
Extract "EDITION_ID" as "reading.txt" / Експортуй текст "EDITION_ID" як "reading.txt"
Backup "backup-name" / Резервна копія "backup-name"
Produce audio: [script text] / Озвуч: [текст сценарію]
Use typed JSON for exact reader edition/revision/range, saved evidence IDs/exports,
reviewed audio conversion hashes/settings, structured audio.render scripts,
and player.control with an explicitly reviewed session, source_sha256 and action pause/stop.
Paths must be inside your selected input/output folders. No delete, shell or remote tools are available.'''


def preview(value, scope):
    text(value, 'assistant instruction', maximum=4 * 1024 * 1024)
    if value.lstrip().startswith('{'):
        from ..durable_state import _unique
        proposed = json.loads(value, object_pairs_hook=_unique)
        from .templates import REQUEST_SCHEMA, plan_from_request
        if isinstance(proposed, dict) and proposed.get('schema') == REQUEST_SCHEMA:
            return plan_from_request(proposed, scope)
        return Plan.from_dict(proposed, scope=scope)
    steps = []
    lines = value.strip().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        # The quoted payload is data. In particular, it is never re-parsed as commands.
        patterns = (
            (r'(?:create\s+(?:a\s+)?library|створи\s+бібліотеку)\s+"([^"]+)"\.?', 'library.create', 'name'),
            (r'(?:import\s+folder|імпортуй\s+папку)\s+"([^"]+)"\.?', 'library.import_folder', 'path'),
            (r'(?:import|імпортуй)\s+"([^"]+)"\.?', 'library.import', 'path'),
            (r'(?:search(?:\s+for)?|знайди|пошук)\s+"([^"]+)"\.?', 'library.search', 'query'),
            (r'(?:backup|резервна\s+копія)\s+"([^"]+)"\.?', 'library.backup', 'name'),
            (r'(?:inspect\s+audio|перевір\s+аудіо)\s+"([^"]+)"\.?', 'audio.inspect', 'path'),
            (r'(?:search\s+evidence|знайди\s+докази)\s+"([^"]+)"\.?', 'evidence.search', 'query'),
        )
        matched = False
        for pattern, tool, key in patterns:
            match = re.fullmatch(pattern, line, re.IGNORECASE)
            if match:
                steps.append(Step(tool, {key: match.group(1)}))
                matched = True
                break
        if matched:
            continue
        if re.fullmatch(r'(?:list\s+(?:all\s+)?libraries|покажи\s+бібліотеки)\.?', line, re.IGNORECASE):
            steps.append(Step('library.list'))
            continue
        if re.fullmatch(r'(?:list\s+books|покажи\s+книги)\.?', line, re.IGNORECASE):
            steps.append(Step('library.catalog'))
            continue
        if re.fullmatch(r'(?:list\s+voices|покажи\s+голоси)\.?', line, re.IGNORECASE):
            steps.append(Step('voice.list'))
            continue
        if re.fullmatch(r'(?:audio\s+effects|покажи\s+аудіоефекти)\.?', line, re.IGNORECASE):
            steps.append(Step('audio.effects'))
            continue
        if re.fullmatch(r'(?:player\s+status|стан\s+програвача)\.?', line, re.IGNORECASE):
            steps.append(Step('player.status'))
            continue
        match = re.fullmatch(r'(?:extract|експортуй\s+текст)\s+"([^"]+)"\s+(?:as|як)\s+"([^"]+)"\.?', line, re.IGNORECASE)
        if match:
            steps.append(Step('library.extract', {'edition': match.group(1), 'name': match.group(2)}))
            continue
        match = re.match(r'(?:produce\s+audio|озвуч|створи\s+аудіо)\s*:\s*(.*)', line, re.IGNORECASE)
        if match:
            script = '\n'.join([match.group(1), *lines[index:]]).strip()
            text(script, 'audio script', maximum=4 * 1024 * 1024)
            steps.append(Step('audio.produce', {'text': script}))
            break
        raise ValueError(f'Command {index} is ambiguous or unsupported. Use the guided command examples; nothing has run.')
    summary = '\n'.join(f'{i + 1}. {s.tool}: ' + json.dumps(s.arguments, ensure_ascii=False)
                        for i, s in enumerate(steps))
    return Plan(scope, tuple(steps), summary[:16384])
